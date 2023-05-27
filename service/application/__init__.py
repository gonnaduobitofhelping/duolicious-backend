import os
from flask import request
import duotypes as t
from service import person, question, location
from database import transaction
import psycopg
from service.application.decorators import (
    app,
    adelete,
    aget,
    apatch,
    apost,
    aput,
    delete,
    get,
    patch,
    post,
    put,
    validate,
)

_init_sql_file = os.path.join(
        os.path.dirname(__file__), '..', '..',
        'init.sql')

def init_db():
    with open(_init_sql_file, 'r') as f:
        init_sql_file = f.read()

    with transaction() as tx:
        try:
            tx.execute(init_sql_file)
        except psycopg.errors.DuplicateTable as e:
            print(e)

@post('/request-otp')
@validate(t.PostRequestOtp)
def post_request_otp(req: t.PostRequestOtp):
    return person.post_request_otp(req)

@post('/check-otp')
@validate(t.PostCheckOtp)
def post_check_otp(req: t.PostCheckOtp):
    return person.post_check_otp(req)

@apost('/check-session-token', expected_onboarding_status=None)
def post_check_session_token(s: t.SessionInfo):
    return dict(onboarded=s.onboarded)

@apatch('/onboardee-info', expected_onboarding_status=False)
@validate(t.PatchOnboardeeInfo)
def patch_onboardee_info(req: t.PatchOnboardeeInfo, s: t.SessionInfo):
    return person.patch_onboardee_info(req, s)

@adelete('/onboardee-info', expected_onboarding_status=False)
@validate(t.DeleteOnboardeeInfo)
def delete_onboardee_info(req: t.DeleteOnboardeeInfo, s: t.SessionInfo):
    return person.delete_onboardee_info(req, s)

@apost('/complete-onboarding', expected_onboarding_status=False)
def post_complete_onboarding(s: t.SessionInfo):
    return person.post_complete_onboarding(s)

@aget('/next-questions')
def get_next_questions(s: t.SessionInfo):
    return question.get_next_questions(s, request.args.get('n', 10))

@put('/answer')
@validate(t.PutAnswer)
def put_answer(req: t.PutAnswer):
    return person.put_answer(req)

@delete('/answer')
@validate(t.DeleteAnswer)
def delete_answer(req: t.DeleteAnswer):
    return person.delete_answer(req)

@get('/personality/<int:person_id>')
def get_personality(person_id):
    return person.get_personality(person_id)

init_db()
location.init_db()
question.init_db()
person.init_db()
