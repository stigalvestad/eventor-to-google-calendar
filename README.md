## Organisation of events in calenders

Groups in Grane:

 - Rekrutt: add directly to calender
 - Ungdom: add directly to calender
 - Styret: add directly to calender
 
Activities in the area, one Google Calender each:

 - Aust-Agder eventor activities
 - Vest-Agder eventor activities
 - Telemark eventor activities
 - Bedriftsløp - different data source, not prioritized

What kind of configuration is needed?

 - List of Key-Value pairs: Organisation id - Google calender-id, assuming Google calendar
 is owned by person running app

## What do I need?

A scheduled task which reads eventor-data for a given period and filter, then checks
to see if the calendars need to be updated. 

Read eventor-data: a lambda which is triggered by a scheduler. 
Can be triggered by an api to begin with.


Eventor arrangementer

|
v
 
Google kalender <-> Grane Facebook Arrangementer

|
v

Grane hjemmeside viser kalender


## Dev environment
Virtualenv for python has been setup in:

    /home/stig/code/python-virtualenv/chalice

This was used to create the environment (one time):

    virtualenv --python $(which python3.6) /home/stig/code/python-virtualenv/chalice
    
To activate environment:

    source /home/stig/code/python-virtualenv/chalice/bin/activate
    
## Managing dependencies
If a new import is required:

- add the import
- install it with pip inside the virtual environment
- finally add it to the requirements file with command below


    pip freeze > requirements.txt
    
The requirements-file will be used by chalice to include the required files when 
uploading the deployment package to AWS.    
    
 
## Example usage

put data in:

    curl -H 'Content-Type: application/json' -X PUT -d '{"myname": "stig"}' https://0b205youuh.execute-api.eu-west-1.amazonaws.com/api/objects/keyB

get data out:

    curl https://wg1q4tw6bh.execute-api.eu-west-1.amazonaws.com/api/objects/keyA

## Preparations 

Upload credentials to s3 before starting service:

    aws s3api put-object --bucket eventor-google-calendar --key calendar-client-secrets --body client_secrets.json

Upload config to s3:

    aws s3api put-object --bucket eventor-google-calendar --key calendar-config --body config/eventor_calendar_config.json

## Deploy

Remember every time we deploy for the first time:

- update BASE_URL in app.py
- generate new client_secrets.json with correct oath2callback url in Google Console (https://console.developers.google.com/)
- upload newly generated secrets-file to S3.
- ensure to delete any credentials- or state- objects in s3 from previous runs

Run this, to ensure access-policy is as wanted (remember to update BASE_URL in source):

    chalice deploy --no-autogen-policy
    
It will use the policy which is described in policy-dev.json