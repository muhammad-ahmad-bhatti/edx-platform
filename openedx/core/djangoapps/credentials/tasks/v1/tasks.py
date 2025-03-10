"""
This file contains celery tasks for credentials-related functionality.
"""

import math
import time

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from celery.utils.log import get_task_logger
from celery_utils.logged_task import LoggedTask
from django.conf import settings
from django.contrib.auth.models import User  # lint-amnesty, pylint: disable=imported-auth-user
from django.contrib.sites.models import Site
from edx_django_utils.monitoring import set_code_owner_attribute
from opaque_keys.edx.keys import CourseKey
from MySQLdb import OperationalError

from common.djangoapps.course_modes.models import CourseMode
from lms.djangoapps.certificates.api import get_recently_modified_certificates
from lms.djangoapps.grades.api import CourseGradeFactory, get_recently_modified_grades
from lms.djangoapps.certificates.models import CertificateStatuses, GeneratedCertificate
from openedx.core.djangoapps.catalog.utils import get_programs
from openedx.core.djangoapps.credentials.helpers import is_learner_records_enabled_for_org
from openedx.core.djangoapps.credentials.models import CredentialsApiConfig
from openedx.core.djangoapps.credentials.utils import get_credentials_api_client
from openedx.core.djangoapps.programs.signals import handle_course_cert_changed, handle_course_cert_awarded
from openedx.core.djangoapps.site_configuration.models import SiteConfiguration

logger = get_task_logger(__name__)

# "interesting" here means "credentials will want to know about it"
INTERESTING_MODES = CourseMode.CERTIFICATE_RELEVANT_MODES
INTERESTING_STATUSES = [
    CertificateStatuses.notpassing,
    CertificateStatuses.downloadable,
]

# Maximum number of retries before giving up.
# For reference, 11 retries with exponential backoff yields a maximum waiting
# time of 2047 seconds (about 30 minutes). Setting this to None could yield
# unwanted behavior: infinite retries.
MAX_RETRIES = 11


@shared_task(bind=True, ignore_result=True)
@set_code_owner_attribute
def send_grade_to_credentials(self, username, course_run_key, verified, letter_grade, percent_grade):
    """ Celery task to notify the Credentials IDA of a grade change via POST. """
    logger.info(f"Running task send_grade_to_credentials for username {username} and course {course_run_key}")

    countdown = 2 ** self.request.retries
    course_key = CourseKey.from_string(course_run_key)

    try:
        credentials_client = get_credentials_api_client(
            User.objects.get(username=settings.CREDENTIALS_SERVICE_USERNAME),
            org=course_key.org,
        )

        credentials_client.grades.post({
            'username': username,
            'course_run': str(course_key),
            'letter_grade': letter_grade,
            'percent_grade': percent_grade,
            'verified': verified,
        })

        logger.info(f"Sent grade for course {course_run_key} to user {username}")

    except Exception as exc:  # lint-amnesty, pylint: disable=unused-variable
        error_msg = f"Failed to send grade for course {course_run_key} to user {username}."
        logger.exception(error_msg)
        exception = MaxRetriesExceededError(
            f"Failed to send grade to credentials. Reason: {error_msg}"
        )
        raise self.retry(exc=exception, countdown=countdown, max_retries=MAX_RETRIES)


@shared_task(base=LoggedTask, ignore_result=True)
@set_code_owner_attribute
def handle_notify_credentials(options, course_keys):
    """
    Celery task to handle the notify_credentials management command. Finds the
    relevant cert and grade records, then starts other celery tasks to send the
    data.
    """

    try:
        site_config = SiteConfiguration.objects.get(site__domain=options['site']) if options['site'] else None
    except SiteConfiguration.DoesNotExist:
        logger.exception('No site configuration found for site %s', options['site'])
        return

    certs = get_recently_modified_certificates(
        course_keys, options['start_date'], options['end_date'], options['user_ids']
    )

    users = None
    if options['user_ids']:
        users = User.objects.filter(id__in=options['user_ids'])

    grades = get_recently_modified_grades(
        course_keys, options['start_date'], options['end_date'], users
    )

    logger.info('notify_credentials Sending notifications for {certs} certificates and {grades} grades'.format(
        certs=certs.count(),
        grades=grades.count()
    ))

    if options['dry_run']:
        log_dry_run(certs, grades)
    else:
        send_notifications(
            certs,
            grades,
            site_config=site_config,
            delay=options['delay'],
            page_size=options['page_size'],
            verbose=options['verbose'],
            notify_programs=options['notify_programs']
        )

    logger.info('notify_credentials finished')


def send_notifications(
    certs, grades, site_config=None, delay=0, page_size=100, verbose=False, notify_programs=False
):
    """ Run actual handler commands for the provided certs and grades. """
    course_cert_info = {}
    # First, do certs
    for i, cert in paged_query(certs, delay, page_size):
        if site_config and not site_config.has_org(cert.course_id.org):
            logger.info("Skipping credential changes %d for certificate %s", i, certstr(cert))
            continue

        logger.info(
            "Handling credential changes %d for certificate %s",
            i, certstr(cert),
        )

        signal_args = {
            'sender': None,
            'user': cert.user,
            'course_key': cert.course_id,
            'mode': cert.mode,
            'status': cert.status,
            'verbose': verbose,
        }

        data = {
            'mode': cert.mode,
            'status': cert.status
        }

        course_cert_info[(cert.user.id, str(cert.course_id))] = data
        handle_course_cert_changed(**signal_args)
        if notify_programs and CertificateStatuses.is_passing_status(cert.status):
            handle_course_cert_awarded(**signal_args)

    # Then do grades
    for i, grade in paged_query(grades, delay, page_size):
        if site_config and not site_config.has_org(grade.course_id.org):
            logger.info("Skipping grade changes %d for grade %s", i, gradestr(grade))
            continue

        logger.info(
            "Handling grade changes %d for grade %s",
            i, gradestr(grade),
        )

        user = User.objects.get(id=grade.user_id)

        # Grab mode/status from cert call
        key = (user.id, str(grade.course_id))
        cert_info = course_cert_info.get(key, {})
        mode = cert_info.get('mode', None)
        status = cert_info.get('status', None)

        send_grade_if_interesting(
            user,
            grade.course_id,
            mode,
            status,
            grade.letter_grade,
            grade.percent_grade,
            verbose=verbose
        )


def paged_query(queryset, delay, page_size):
    """
    A generator that iterates through a queryset but only resolves chunks of it at once, to avoid overwhelming memory
    with a giant query. Also adds an optional delay between yields, to help with load.
    """
    count = queryset.count()
    pages = int(math.ceil(count / page_size))

    for page in range(pages):
        page_start = page * page_size
        page_end = page_start + page_size
        subquery = queryset[page_start:page_end]

        if delay and page:
            time.sleep(delay)
        index = 0
        try:
            for item in subquery.iterator():
                index += 1
                yield page_start + index, item
        except OperationalError:
            # When running the notify_credentials command locally there is an
            # OperationalError thrown by MySQL when there are no more results
            # available for the queryset iterator. This change catches that exception,
            # checks state, and then logs according to that state. This code runs in
            # production without issue. This changes allows for the code to be run
            # locally without a separate code path.
            if index == count:
                logger.info('OperationalError Exception caught, all known results processed in paged_query')
            else:
                logger.warning('OperationalError Exception caught, it is possible some results were missed')
            continue


def log_dry_run(certs, grades):
    """Give a preview of what certs/grades we will handle."""
    logger.info("DRY-RUN: This task would have handled changes for...")
    ITEMS_TO_SHOW = 10

    logger.info(f"{certs.count()} Certificates:")
    for cert in certs[:ITEMS_TO_SHOW]:
        logger.info(f"   {certstr(cert)}")
    if certs.count() > ITEMS_TO_SHOW:
        logger.info(f"    (+ {certs.count() - ITEMS_TO_SHOW} more)")

    logger.info(f"{grades.count()} Grades:")
    for grade in grades[:ITEMS_TO_SHOW]:
        logger.info(f"   {gradestr(grade)}")
    if grades.count() > ITEMS_TO_SHOW:
        logger.info(f"    (+ {grades.count() - ITEMS_TO_SHOW} more)")


def certstr(cert):
    return f'{cert.course_id} for user {cert.user.id}'


def gradestr(grade):
    return f'{grade.course_id} for user {grade.user_id}'


# This has Credentials business logic that has bled into the LMS. But we want to filter here in order to
# not flood our task queue with a bunch of signals. So we put up with it.
def send_grade_if_interesting(user, course_run_key, mode, status, letter_grade, percent_grade, verbose=False):
    """ Checks if grade is interesting to Credentials and schedules a Celery task if so. """

    if verbose:
        msg = "Starting send_grade_if_interesting with params: "\
            "user [{username}], "\
            "course_run_key [{key}], "\
            "mode [{mode}], "\
            "status [{status}], "\
            "letter_grade [{letter_grade}], "\
            "percent_grade [{percent_grade}], "\
            "verbose [{verbose}]"\
            .format(
                username=getattr(user, 'username', None),
                key=str(course_run_key),
                mode=mode,
                status=status,
                letter_grade=letter_grade,
                percent_grade=percent_grade,
                verbose=verbose
            )
        logger.info(msg)
    # Avoid scheduling new tasks if certification is disabled. (Grades are a part of the records/cert story)
    if not CredentialsApiConfig.current().is_learner_issuance_enabled:
        if verbose:
            logger.info("Skipping send grade: is_learner_issuance_enabled False")
        return

    # Avoid scheduling new tasks if learner records are disabled for this site.
    if not is_learner_records_enabled_for_org(course_run_key.org):
        if verbose:
            logger.info(
                "Skipping send grade: ENABLE_LEARNER_RECORDS False for org [{org}]".format(
                    org=course_run_key.org
                )
            )
        return

    # Grab mode/status if we don't have them in hand
    if mode is None or status is None:
        try:
            cert = GeneratedCertificate.objects.get(user=user, course_id=course_run_key)  # pylint: disable=no-member
            mode = cert.mode
            status = cert.status
        except GeneratedCertificate.DoesNotExist:
            # We only care about grades for which there is a certificate.
            if verbose:
                logger.info(
                    "Skipping send grade: no cert for user [{username}] & course_id [{course_id}]".format(
                        username=getattr(user, 'username', None),
                        course_id=str(course_run_key)
                    )
                )
            return

    # Don't worry about whether it's available as well as awarded. Just awarded is good enough to record a verified
    # attempt at a course. We want even the grades that didn't pass the class because Credentials wants to know about
    # those too.
    if mode not in INTERESTING_MODES or status not in INTERESTING_STATUSES:
        if verbose:
            logger.info(
                "Skipping send grade: mode/status uninteresting for mode [{mode}] & status [{status}]".format(
                    mode=mode,
                    status=status
                )
            )
        return

    # If the course isn't in any program, don't bother telling Credentials about it. When Credentials grows support
    # for course records as well as program records, we'll need to open this up.
    if not is_course_run_in_a_program(course_run_key):
        if verbose:
            logger.info(
                f"Skipping send grade: course run not in a program. [{str(course_run_key)}]"
            )
        return

    # Grab grades if we don't have them in hand
    if letter_grade is None or percent_grade is None:
        grade = CourseGradeFactory().read(user, course_key=course_run_key, create_if_needed=False)
        if grade is None:
            if verbose:
                logger.info(
                    "Skipping send grade: No grade found for user [{username}] & course_id [{course_id}]".format(
                        username=getattr(user, 'username', None),
                        course_id=str(course_run_key)
                    )
                )
            return
        letter_grade = grade.letter_grade
        percent_grade = grade.percent

    send_grade_to_credentials.delay(user.username, str(course_run_key), True, letter_grade, percent_grade)


def is_course_run_in_a_program(course_run_key):
    """ Returns true if the given course key is in any program at all. """

    # We don't have an easy way to go from course_run_key to a specific site that owns it. So just search each site.
    sites = Site.objects.all()
    str_key = str(course_run_key)
    for site in sites:
        for program in get_programs(site):
            for course in program['courses']:
                for course_run in course['course_runs']:
                    if str_key == course_run['key']:
                        return True
    return False
