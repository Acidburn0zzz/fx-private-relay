from datetime import datetime, timedelta

from decouple import config
from phonenumbers import parse, format_number
from phonenumbers import PhoneNumberFormat
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import MultipleObjectsReturned, PermissionDenied
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt

from privaterelay.utils import (
    incr_if_enabled,
    get_post_data_from_request,
    get_user_profile
)

from .models import Session
from .utils import get_available_numbers


account_sid = config('TWILIO_ACCOUNT_SID', None)
auth_token = config('TWILIO_AUTH_TOKEN', None)
client = Client(account_sid, auth_token)
service = client.proxy.services(config('TWILIO_SERVICE_ID'))


PROMPT_MESSAGE = "For how many minutes do you need a relay number?"


@csrf_exempt
def index(request):
    incr_if_enabled('phones_index', 1)
    request_data = get_post_data_from_request(request)
    is_validated_create = (
        request_data.get('method_override', None) is None and
        request_data.get("api_token", False)
    )
    is_validated_user = (
        request.user.is_authenticated and
        request_data.get("api_token", False)
    )
    if is_validated_create:
        return _index_POST(request)
    if not is_validated_user:
        return redirect('profile')
    if request.method == 'POST':
        return _index_POST(request)
    incr_if_enabled('emails_index_get', 1)
    return redirect('profile')


def _index_POST(request):
    request_data = get_post_data_from_request(request)
    api_token = request_data.get('api_token', None)
    if not api_token:
        raise PermissionDenied
    user_profile = get_user_profile(request, api_token)

    incr_if_enabled('phones_index_post', 1)
    existing_sessions = Session.objects.filter(user=user_profile.user)
    if existing_sessions.count() >= settings.MAX_NUM_PHONE_SESSIONS:
        if 'moz-extension' in request.headers.get('Origin', ''):
            return HttpResponse('Payment Required', status=402)
        messages.error(
            request, "You already have %s phone sessions. Please upgrade." %
            settings.MAX_NUM_PHONE_SESSIONS
        )
        return redirect('profile')

    session = Session.make_session_for_user(user_profile.user)
    if 'moz-extension' in request.headers.get('Origin', ''):
        return JsonResponse({
            'id': session.id,
            'phone_num': address_string
        }, status=201)

    return redirect('profile')


@csrf_exempt
def main_twilio_webhook(request):
    resp = MessagingResponse()
    from_num = request.POST['From']
    body = request.POST['Body'].lower()

    Session.delete_expired_sessions()

    try:
        ttl_minutes = int(body)
    except ValueError:
        resp.message(PROMPT_MESSAGE)
        return HttpResponse(resp)

    _reset_numbers_sessions(from_num)

    # Check that there are relay numbers available so we don't accidentally
    # create an open session that will be crossed with some other session.
    available_numbers = get_available_numbers()

    session, participant = get_session_and_participant_with_available_number(
        available_numbers, ttl_minutes, from_num
    )
    proxy_num = participant.proxy_identifier

    # store this half-way open session in our local DB,
    # so when the next number texts the proxy number,
    # we can add them as the 2nd participant and open the session
    expiration_datetime = session.date_created + timedelta(minutes=ttl_minutes)
    db_session = Session.objects.create(
        twilio_sid = session.sid,
        initiating_proxy_number=proxy_num,
        initiating_real_number=from_num,
        initiating_participant_sid=participant.sid,
        status='waiting-for-party',
        expiration=expiration_datetime,
    )

    # reply back with the number and minutes it will live
    pretty_from = format_number(parse(from_num), PhoneNumberFormat.NATIONAL)
    pretty_proxy = format_number(parse(proxy_num), PhoneNumberFormat.NATIONAL)
    resp.message(
        '%s will forward to this number for %s minutes' %
        (pretty_proxy, ttl_minutes)
    )
    return HttpResponse(resp)


@csrf_exempt
def twilio_proxy_out_of_session(request):
    """
    By design, Relay doesn't know who the 2nd participant is before they text
    the 1st participant.

    Twilio requires both participants be added to a session before either can
    communicate with the other. When the 2nd participant sends their first text
    to a 1st participant, it triggers an "out-of-session" hook.

    So, we use this to add the 2nd participant to the existing session, relay
    the 2nd participant's message, and the 2 parties can begin communicating
    thru the proxy number.
    """

    resp = MessagingResponse()

    _delete_expired_sessions()
    try:
        db_session = Session.objects.get(
            initiating_proxy_number=request.POST['To'],
            status='waiting-for-party',
        )
    except Session.DoesNotExist as e:
        print(e)
        resp.message('The number you are trying to reach is unavailable.')
        return HttpResponse(resp)
    except MultipleObjectsReturned as e:
        print(e)
        resp.message('The number you are trying to reach is busy.')
        return HttpResponse(resp)


    twilio_session = service.sessions(db_session.twilio_sid).fetch()
    if (twilio_session.status in ['closed', 'failed', 'unknown']):
        error_message = ('Twilio session %s status: %s' %
                         (db_session.twilio_sid, twilio_session.status))
        print(error_message)
        return HttpResponseNotFound(error_message)

    from_num = request.POST['From']
    new_participant = service.sessions(twilio_session.sid).participants.create(
        identifier=from_num,
        proxy_identifier=db_session.initiating_proxy_number,
    )
    # Now that the twilio session is in-progress, we can delete our DB record
    db_session.delete()

    # Now that we've added the 2nd participant,
    # send their first message to the 1st participant
    message = (
        service.sessions(twilio_session.sid)
        .participants(db_session.initiating_participant_sid)
        .message_interactions.create(body=request.POST['Body'])
    )
    return HttpResponse(status=201, content="Created")


def _reset_numbers_sessions(number):
    # If this number already has any sessions, close them on Twilio
    # and delete them from the local DB
    from_num_sessions = Session.objects.filter(
        initiating_real_number=number
    )
    for session in from_num_sessions:
        service.sessions(session.twilio_sid).update(status='closed')
    from_num_sessions.delete()

def _delete_expired_sessions():
    expired_sessions = Session.objects.filter(
        status='waiting-for-party',
        expiration__lte=datetime.now()
    )
    expired_sessions.delete()
