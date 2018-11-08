
# TODO I believe I can optimize imports, to reduce size of deployment zip file (eventor-calendar-bridge > .chalice > deployments)
# currently it is 9.2 MB - try to get it smaller, while still making things work
import logging
import google.oauth2
import requests
import untangle
# import google.oauth2.credentials
import google_auth_oauthlib.flow
import googleapiclient.discovery
import datetime

import json
import boto3
from botocore.exceptions import ClientError

from chalice import NotFoundError, Response
from chalice import Chalice, Rate


app = Chalice(app_name='eventor-calendar-bridge')
app.debug = True
app.log.setLevel(logging.DEBUG)

S3 = boto3.client('s3', region_name='eu-west-1')
BUCKET = 'eventor-google-calendar'

# TODO this is bad, should find a better way -> use env variables
BASE_URL = 'https://k5snffugl5.execute-api.eu-west-1.amazonaws.com' + '/api'

# This OAuth 2.0 access scope allows for full read/write access to the
# authenticated user's account and requires requests to use an SSL connection.
# TODO At the moment, need both scopes, even though I only wanted the first. Seems the second is sticking in google responses
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/drive.metadata.readonly', 'https://www.googleapis.com/auth/calendar']
API_SERVICE_NAME = 'calendar'
API_VERSION = 'v3'

# This variable specifies the name of a file that contains the OAuth 2.0
# information for this application, including its client_id and client_secret.
CLIENT_SECRETS_FILE = "client_secrets.json"

CALENDAR_ID = '4dd5j9sjb8q9sdf3hbeuig0b3g@group.calendar.google.com'


API_KEY = '56475dea313348cea260e8e9035469f5'
EVENTOR_BASE_URL = 'https://eventor.orientering.no/api'

eventor_headers = {
    'ApiKey': API_KEY
}


def build_eventor_api_url(relative_path):
    return EVENTOR_BASE_URL + relative_path


def get_from_eventor(path):
    app.log.debug('Fetching events from eventor: ' + path)
    response = requests.request('GET', build_eventor_api_url(path),
                                headers=eventor_headers)
    # app.log.debug('Got response: ' + response.text)
    response.raise_for_status()
    return response.text


def int_list_to_comma_sep_string(int_list):
    return ','.join(map(str, int_list))


def get_events_from_eventor(organisation_ids):
    organisation_ids_comma_sep = int_list_to_comma_sep_string(organisation_ids)
    format_date = '{0:%Y-%m-%d}'
    now = datetime.datetime.now()
    future = get_future_date(now)
    xml_text = get_from_eventor('/events?fromDate=' + format_date.format(now) + '&toDate=' + format_date.format(future)
                                + '&organisationIds=' + organisation_ids_comma_sep)
    event_list = untangle.parse(xml_text)
    calendar_events = []
    for event in event_list.EventList.Event:
        # app.log.debug('---------------')
        # app.log.debug('Event: ' + event.Name.cdata)
        # app.log.debug('Event-id: ' + event.EventId.cdata)
        # app.log.debug('Start dato: ' + event.StartDate.Date.cdata)
        # app.log.debug('Start klokke: ' + event.StartDate.Clock.cdata)
        # app.log.debug('Stopp dato: ' + event.FinishDate.Date.cdata)
        # app.log.debug('Stopp klokke: ' + event.FinishDate.Clock.cdata)
        # app.log.debug('EventCenterPosition-x: ' + event.EventRace.get('EventCenterPosition', {'x': 8}))
        # app.log.debug('EventCenterPosition-y: ' + event.EventRace.EventCenterPosition['y'])
        # app.log.debug('EventCenterPosition-unit: ' + event.EventRace.EventCenterPosition['unit'])
        event_id = event.EventId.cdata
        link = 'http://eventor.orientering.no/Events/Show/' + event_id
        location = ''
        try:
            location = event.EventRace.EventCenterPosition['y'] + ', ' + event.EventRace.EventCenterPosition['x']
        except:
            app.log.debug('EventCenterPosition is unavailable for event  ' + event_id)

        start_time = event.StartDate.Date.cdata + 'T' + event.StartDate.Clock.cdata
        end_time = event.FinishDate.Date.cdata + 'T' + event.FinishDate.Clock.cdata
        calendar_event = {
            'summary': event.Name.cdata,
            'location': location,
            'description': 'Se flere detaljer i Eventor: ' + link,
            'start': {
                'dateTime': start_time,
                'timeZone': 'Europe/Paris',
            },
            'end': {
                'dateTime': end_time,
                'timeZone': 'Europe/Paris',
            },
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'popup', 'minutes': 24 * 60}
                ],
            },
            'source': {
                'title': event_id,
                'url': link
            },
            'transparency': 'transparent',
            'visibility': 'public'
        }
        calendar_events.append(calendar_event)
    app.log.debug('Found ' + str(len(calendar_events)) + ' events in Eventor for these org ids: '
                  + organisation_ids_comma_sep)
    return calendar_events


def get_code_from_request():
    req_details = app.current_request.to_dict()
    code = req_details['query_params']['code']
    return code


# Automatically runs every day
@app.schedule(Rate(60 * 24, unit=Rate.MINUTES))
def periodic_task(event):
    return sync_eventor_with_google_calendar()


@app.route('/sync-google-eventor')
def sync_eventor_with_google_calendar():
    app.log.info('------method: sync-google-eventor')
    credentials_json_string = get_from_s3_safe(BUCKET, 'credentials', False)
    if not credentials_json_string:
        full_url = BASE_URL + '/authorize'
        app.log.debug('Credentials missing, redirecting to: ' + full_url)
        return make_redirect_response(BASE_URL + '/authorize')

    app.log.debug('Credentials exist, proceeding with google API request')
    cred_as_dict = json.loads(credentials_json_string)
    credentials = google.oauth2.credentials.Credentials(**cred_as_dict)

    calendar_client = googleapiclient.discovery.build(
        API_SERVICE_NAME, API_VERSION, credentials=credentials)

    config_json_string = get_from_s3_safe(BUCKET, 'calendar-config', False)
    config = json.loads(config_json_string)

    processed_events = 0
    for calendar_config in config['calendar_config']:
        events = add_to_one_calendar(calendar_client, calendar_config['calendar_id'], calendar_config['organisation_ids'])
        processed_events += len(events)

    # Save credentials in case access token was refreshed.
    updated_credentials = credentials_to_dict(credentials)
    add_to_s3(BUCKET, 'credentials', json.dumps(updated_credentials))

    app.log.debug('Events synched successfully')

    return json.loads(json.dumps({'events_processed': processed_events, 'status': 'OK'}))


def add_to_one_calendar(calendar_client, calendar_id, organisation_ids):
    app.log.info('Start syncing events for organisation(s) ' + int_list_to_comma_sep_string(organisation_ids)
                 + ' => Calendar ' + calendar_id)
    existing_events = find_events(calendar_client, calendar_id)
    eventor_calendar_events = get_events_from_eventor(organisation_ids)
    for eventor_calendar_event in eventor_calendar_events:
        if event_already_exists(eventor_calendar_event, existing_events):
            # TODO check if event has changed - if it has, update it - either delete and insert or patch
            app.log.debug('TODO check if event has changed.')
        else:
            insert_event(calendar_client, eventor_calendar_event, calendar_id)

    return eventor_calendar_events


def event_already_exists(event, existing_events):
    for existing_event in existing_events:
        this_event_id = event['source']['title']
        this_existing_source = existing_event.get('source', {'title': None})
        if this_event_id == this_existing_source.get('title'):
            app.log.debug('this event already exists - dont add it again: ' + this_event_id)
            return True
    return False


def format_utc(a_date):
    return a_date.isoformat() + 'Z' # 'Z' indicates UTC time


def get_future_date(from_date):
    return from_date + datetime.timedelta(weeks=+40)


def find_events(calendar_client, calendar_id):
    now = datetime.datetime.utcnow()
    future = get_future_date(now)
    app.log.info('Getting the upcoming events from Google Calendar, 40 weeks ahead, for calendar: ' + calendar_id)
    page_size = 50
    events_result = calendar_client.events().list(calendarId=calendar_id, timeMin=format_utc(now),
                                                  timeMax=format_utc(future), maxResults=page_size, singleEvents=True,
                                                  orderBy='startTime').execute()
    all_events = []
    while events_result.get('nextPageToken', False):
        app.log.debug('Fetching next page')
        next_page_token = events_result.get('nextPageToken', False)
        all_events.extend(events_result.get('items', []))
        events_result = calendar_client.events().list(calendarId=calendar_id, timeMin=format_utc(now),
                                              timeMax=format_utc(future), maxResults=page_size, singleEvents=True,
                                              orderBy='startTime', pageToken=next_page_token).execute()
    all_events.extend(events_result.get('items', []))
    app.log.info('Total events found: ' + str(len(all_events)))

    for event in all_events:
        app.log.debug(event['summary'])
        app.log.debug(event.get('source'))
    return all_events


def insert_event(calendar_service, event, calendar_id):
    event = calendar_service.events().insert(calendarId=calendar_id, body=event).execute()
    app.log.info('Event created: %s' % (event.get('htmlLink')))


@app.route('/authorize')
def authorize():
    app.log.info('------method: authorize')
    secrets_raw = get_from_s3(BUCKET, 'calendar-client-secrets')
    secrets = json.loads(secrets_raw)

    app.log.debug('secrets: ' + json.dumps(secrets))

    # Create flow instance to manage the OAuth 2.0 Authorization Grant Flow steps.
    flow = google_auth_oauthlib.flow.Flow.from_client_config(secrets, scopes=SCOPES)
    flow.redirect_uri = BASE_URL + '/oauth2callback'

    app.log.debug('redirect_uri: ' + flow.redirect_uri)
    authorization_url, state = flow.authorization_url(
        # Enable offline access so that you can refresh an access token without
        # re-prompting the user for permission. Recommended for web server apps.
        access_type='offline',
        # Enable incremental authorization. Recommended as a best practice.
        include_granted_scopes='true')

    app.log.debug('Current state: ' + state)
    app.log.debug('Current authorization url: ' + authorization_url)

    # Store the state so the callback can verify the auth server response.
    add_to_s3(BUCKET, 'state', state)
    return make_redirect_response(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    app.log.info('------method: oauth2callback')
    # Specify the state when creating the flow in the callback so that it can
    # verified in the authorization server response.
    req_as_dict = app.current_request.to_dict()
    app.log.debug('state: ' + json.dumps(req_as_dict))

    state_raw = get_from_s3(BUCKET, 'state')
    state = state_raw.decode('utf-8')
    app.log.debug('state: ' + state)

    secrets = json.loads(get_from_s3(BUCKET, 'calendar-client-secrets'))

    # Create flow instance to manage the OAuth 2.0 Authorization Grant Flow steps.
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        secrets, scopes=SCOPES, state=state)
    # # flow.redirect_uri = flask.url_for('oauth2callback', _external=True)
    flow.redirect_uri = BASE_URL + '/oauth2callback'

    # Use the code in the authorization server's response to fetch the OAuth 2.0 tokens.
    flow.fetch_token(code=get_code_from_request())

    # Store credentials
    cred_dict = credentials_to_dict(flow.credentials)
    add_to_s3(BUCKET, 'credentials', json.dumps(cred_dict))

    return make_redirect_response(BASE_URL + '/sync-google-eventor')


def make_redirect_response(location):
    return Response(
        status_code=307,
        body='',
        headers={'Location': location, 'Content-Type': 'text/plain'})


def add_to_s3(bucket, key, body):
    app.log.debug('adding to s3 bucket: ' + bucket + ', key: ' + key)
    S3.put_object(Bucket=bucket, Key=key, Body=body)


def get_from_s3(bucket, key):
    response = S3.get_object(Bucket=bucket, Key=key)
    return response['Body'].read()


def get_from_s3_safe(bucket, key, default):
    try:
        return get_from_s3(bucket, key)
    except ClientError as e:
        return default


def credentials_to_dict(credentials):
    return {'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes}