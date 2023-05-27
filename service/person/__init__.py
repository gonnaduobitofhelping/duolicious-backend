import os
import psycopg
from database import transaction, fetchall_sets
from typing import DefaultDict, Optional, Iterable
import service.question as question
import duotypes as t
import urllib.request
import json
import secrets
from duohash import sha512
from PIL import Image
import io
import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed

EMAIL_KEY = os.environ['DUO_EMAIL_KEY']
EMAIL_URL = os.environ['DUO_EMAIL_URL']

R2_ACCT_ID = os.environ['DUO_R2_ACCT_ID']
R2_ACCESS_KEY_ID = os.environ['DUO_R2_ACCESS_KEY_ID']
R2_ACCESS_KEY_SECRET = os.environ['DUO_R2_ACCESS_KEY_SECRET']
R2_BUCKET_NAME = os.environ['DUO_R2_BUCKET_NAME']

Q_DELETE_ANSWER = """
DELETE FROM answer
WHERE person_id = %(person_id)s
AND question_id = %(question_id)s
"""

Q_SET_ANSWER = """
INSERT INTO answer (
    person_id,
    question_id,
    answer,
    public_
) VALUES (
    %(person_id)s,
    %(question_id)s,
    %(answer)s,
    %(public)s
) ON CONFLICT (person_id, question_id) DO UPDATE SET
    answer  = EXCLUDED.answer,
    public_ = EXCLUDED.public_
"""

Q_SET_PERSON_TRAIT_STATISTIC = """
WITH
existing_answer AS (
    SELECT
        person_id,
        question_id,
        answer
    FROM answer
    WHERE person_id = %(person_id)s
    AND question_id = %(question_id)s
),
score AS (
    SELECT
        person_id,
        trait_id,
        CASE
        WHEN existing_answer.answer = TRUE
            THEN presence_given_yes
            ELSE presence_given_no
        END AS presence_score,
        CASE
        WHEN existing_answer.answer = TRUE
            THEN absence_given_yes
            ELSE absence_given_no
        END AS absence_score
    FROM question_trait_pair
    JOIN existing_answer
    ON existing_answer.question_id = question_trait_pair.question_id
),
score_delta_magnitude AS (
    SELECT
        person_id,
        trait_id,
        presence_score - LEAST(presence_score, absence_score) AS presence_delta_magnitude,
        absence_score  - LEAST(presence_score, absence_score) AS absence_delta_magnitude
    FROM score
),
score_delta AS (
    SELECT
        person_id,
        trait_id,
        %(weight)s * presence_delta_magnitude AS presence_delta,
        %(weight)s * absence_delta_magnitude  AS absence_delta
    FROM score_delta_magnitude
),
new_scores AS (
    SELECT
        sd.person_id,
        sd.trait_id,
        COALESCE(pts.presence_score, 0) + sd.presence_delta,
        COALESCE(pts.absence_score, 0)  + sd.absence_delta
    FROM score_delta sd
    LEFT JOIN person_trait_statistic pts
    ON
        sd.person_id = pts.person_id AND
        sd.trait_id  = pts.trait_id
)
INSERT INTO person_trait_statistic (
    person_id,
    trait_id,
    presence_score,
    absence_score
)
SELECT * FROM new_scores
ON CONFLICT (person_id, trait_id) DO UPDATE SET
    presence_score = EXCLUDED.presence_score,
    absence_score  = EXCLUDED.absence_score
"""

Q_SELECT_PERSONALITY = """
WITH
coalesced AS (
    SELECT
        trait,
        COALESCE(presence_score, 0) AS presence_score,
        COALESCE(absence_score, 0) AS absence_score
    FROM trait
    LEFT JOIN person_trait_statistic
    ON trait.id = person_trait_statistic.trait_id
    WHERE person_id = %(person_id)s
)
SELECT
    trait,
    CASE
    WHEN presence_score + absence_score < 1000
        THEN NULL
        ELSE round(100 * presence_score / (presence_score + absence_score))::int
    END AS percentage
FROM coalesced
"""

Q_DELETE_OTP = """
DELETE FROM prospective_duo_session
WHERE email = %(email)s
"""

Q_SET_OTP = """
INSERT INTO prospective_duo_session (
    email,
    otp
) VALUES (
    %(email)s,
    %(otp)s
)
"""

Q_MAYBE_SET_SESSION_TOKEN_HASH = """
WITH deleted_prospective_duo_session AS (
    DELETE FROM prospective_duo_session
    WHERE email = %(email)s
    AND otp = %(otp)s
    RETURNING expiry
)
INSERT INTO duo_session (
    session_token_hash,
    person_id,
    email
)
SELECT
    %(session_token_hash)s,
    (SELECT id FROM person WHERE email = %(email)s),
    %(email)s
FROM deleted_prospective_duo_session
WHERE deleted_prospective_duo_session.expiry > NOW()
RETURNING session_token_hash, person_id
"""

Q_DELETE_ONBOARDEE_PHOTO = """
DELETE FROM onboardee_photo
WHERE email = %(email)s AND position = %(position)s
RETURNING uuid
"""

Q_COMPLETE_ONBOARDING_1 = """
WITH
new_person AS (
    INSERT INTO person (
        email,
        name,
        date_of_birth,
        location_id,
        gender_id,
        about,

        verified,

        unit_id,

        chats_notification,
        intros_notification,
        visitors_notification
    ) SELECT
        email,
        name,
        date_of_birth,
        location_id,
        gender_id,
        about,

        (SELECT id FROM yes_no WHERE name = 'No'),

        (
            SELECT id
            FROM unit
            WHERE name IN (
                SELECT
                    CASE
                    WHEN country = 'United States'
                        THEN 'Imperial'
                        ELSE 'Metric'
                    END AS name
                FROM location
                JOIN onboardee
                ON location.id = onboardee.location_id
            )
        ),

        (SELECT id FROM immediacy WHERE name = 'Immediately'),
        (SELECT id FROM immediacy WHERE name = 'Immediately'),
        (SELECT id FROM immediacy WHERE name = 'Daily')
    FROM onboardee
    WHERE email = %(email)s
    RETURNING id, email
),
new_photo AS (
    INSERT INTO photo (
        person_id,
        position,
        uuid
    )
    SELECT
        new_person.id,
        position,
        uuid
    FROM onboardee_photo
    JOIN new_person
    ON onboardee_photo.email = new_person.email
    RETURNING person_id
),
new_search_preference_gender AS (
    INSERT INTO search_preference_gender (
        person_id,
        gender_id
    )
    SELECT
        new_person.id,
        gender_id
    FROM onboardee_search_preference_gender
    JOIN new_person
    ON onboardee_search_preference_gender.email = new_person.email
    RETURNING person_id
),
new_question_order_map AS (
    WITH
    row_to_shuffle AS (
      SELECT id
      FROM question
      WHERE id > 50
      ORDER BY RANDOM()
      LIMIT (SELECT ROUND(0.2 * COUNT(*)) FROM question)
    ),
    shuffled_src_to_dst_position AS (
      SELECT
        a.id AS src_position,
        b.id AS dst_position
      FROM (SELECT *, ROW_NUMBER() OVER(ORDER BY RANDOM()) FROM row_to_shuffle) AS a
      JOIN (SELECT *, ROW_NUMBER() OVER(ORDER BY RANDOM()) FROM row_to_shuffle) AS b
      ON a.row_number = b.row_number
    ),
    identity_src_to_dst_position AS (
      SELECT
        id AS src_position,
        id AS dst_position
      FROM question
      WHERE id NOT IN (SELECT src_position FROM shuffled_src_to_dst_position)
    )
    (SELECT * FROM identity_src_to_dst_position)
    UNION
    (SELECT * FROM shuffled_src_to_dst_position)
),
new_question_order AS (
    INSERT INTO question_order (
        person_id,
        question_id,
        position
    ) SELECT
        new_person.id,
        new_question_order_map.src_position,
        new_question_order_map.dst_position
    FROM new_person
    CROSS JOIN new_question_order_map
    RETURNING person_id
),
updated_session AS (
    UPDATE duo_session
    SET person_id = new_person.id
    FROM new_person
    WHERE duo_session.email = new_person.email
    RETURNING person_id
)
SELECT
    (SELECT COUNT(*) FROM new_person) +
    (SELECT COUNT(*) FROM new_photo) +
    (SELECT COUNT(*) FROM new_search_preference_gender) +
    (SELECT COUNT(*) FROM new_question_order) +
    (SELECT COUNT(*) FROM updated_session)
"""

Q_COMPLETE_ONBOARDING_2 = """
DELETE FROM onboardee
WHERE email = %(email)s
"""

s3 = boto3.resource('s3',
  endpoint_url = f'https://{R2_ACCT_ID}.r2.cloudflarestorage.com',
  aws_access_key_id = R2_ACCESS_KEY_ID,
  aws_secret_access_key = R2_ACCESS_KEY_SECRET,
)

bucket = s3.Bucket(R2_BUCKET_NAME)

def init_db():
    with transaction() as tx:
        tx.execute("SELECT COUNT(*) FROM person")
        if tx.fetchone()['count'] != 0:
            return

        tx.execute(
            """
            INSERT INTO person (
                email,
                name,
                date_of_birth,
                location_id,
                gender_id,
                about,

                verified,

                unit_id,

                chats_notification,
                intros_notification,
                visitors_notification
            )
            VALUES (
                %(email)s,
                %(name)s,
                %(date_of_birth)s,
                (SELECT id FROM location LIMIT 1),
                (SELECT id FROM gender LIMIT 1),
                %(about)s,

                (SELECT id FROM yes_no LIMIT 1),

                (SELECT id FROM unit LIMIT 1),

                (SELECT id FROM immediacy LIMIT 1),
                (SELECT id FROM immediacy LIMIT 1),
                (SELECT id FROM immediacy LIMIT 1)
            )
            """,
            dict(
                email='ch.na.ha+testingasdf@gmail.com',
                name='Rahim',
                date_of_birth='1999-05-30',
                about="I'm a reasonable person copypasta",
            )
        )

def process_image(
        image: Image.Image,
        output_size: Optional[int] = None
) -> io.BytesIO:
    output_bytes = io.BytesIO()

    if output_size is not None:
        # Get the dimensions of the image
        width, height = image.size

        # Find the smaller dimension
        min_dim = min(width, height)

        # Compute the area to crop
        left = (width - min_dim) // 2
        top = (height - min_dim) // 2
        right = (width + min_dim) // 2
        bottom = (height + min_dim) // 2

        # Crop the image to be square
        image = image.crop((left, top, right, bottom))

        if output_size != min_dim:
            # Scale the image to the desired size
            image = image.resize((output_size, output_size))

    image = image.convert('RGB')

    image.save(
        output_bytes,
        format='JPEG',
        quality=85,
        subsampling=2,
        progressive=True,
        optimize=True,
    )

    output_bytes.seek(0)

    return output_bytes

def push_to_object_store(io_bytes: io.BytesIO, key: str):
    bucket.put_object(Key=key, Body=io_bytes)

def delete_from_object_store(key: str):
    s3.Object(R2_BUCKET_NAME, key).delete()

def delete_images_from_object_store(uuids: Iterable[str]):
    keys_to_delete = [
        key_to_delete
        for uuid in uuids
        for key_to_delete in [
            f'original-{uuid}.jpg',
            f'450-{uuid}.jpg',
            f'900-{uuid}.jpg']
        if uuid is not None
    ]

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(delete_from_object_store, key)
            for key in keys_to_delete}

        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f'Failed to delete object:', e)


def put_answer(req: t.PutAnswer):
    params = req.dict()

    with transaction() as tx:
        tx.execute(Q_SET_PERSON_TRAIT_STATISTIC, params | {'weight': -1})
        tx.execute(Q_SET_ANSWER, params)
        tx.execute(Q_SET_PERSON_TRAIT_STATISTIC, params | {'weight': +1})


def delete_answer(req: t.DeleteAnswer):
    params = req.dict()

    with transaction() as tx:
        tx.execute(Q_SET_PERSON_TRAIT_STATISTIC, params | {'weight': -1})
        tx.execute(Q_DELETE_ANSWER, params)

def post_request_otp(req: t.PostRequestOtp):
    email = req.email
    otp = '{:06d}'.format(secrets.randbelow(10**6))

    params = dict(
        email=email,
        otp=otp,
    )

    headers = {
        'accept': 'application/json',
        'api-key': EMAIL_KEY,
        'content-type': 'application/json'
    }

    data = {
       "sender": {
          "name": "Duolicious",
          "email": "no-reply@duolicious.app"
       },
       "to": [ { "email": email } ],
       "subject": "Verify Your Email",
       "htmlContent": f"""
<html lang="en">
    <head>
        <title>Verify Your Email</title>
    </head>
    <body>
        <div style="padding: 20px; font-family: Helvetica, sans-serif; background-color: #70f; max-width: 600px; color: white; margin: 40px auto; text-align: center;">
            <p style="color: white; font-weight: 900;">Your Duolicious one-time password is</p>
            <strong style="font-weight: 900; display: inline-block; font-size: 200%; background-color: white; color: #70f; padding: 15px; margin: 10px;">{otp}</strong>
            <p style="color: white; font-weight: 900;">If you didn’t request this, you can ignore this message.</p>
        </div>
    </body>
</html>
"""
    }

    urllib_req = urllib.request.Request(
        EMAIL_URL,
        headers=headers,
        data=json.dumps(data).encode('utf-8')
    )

    # TODO
    # with urllib.request.urlopen(urllib_req) as f:
    #     pass

    with transaction() as tx:
        tx.execute(Q_DELETE_OTP, params)
        tx.execute(Q_SET_OTP, params)

def post_check_otp(req: t.PostCheckOtp):
    session_token = secrets.token_hex(64)
    session_token_hash = sha512(session_token)

    params = dict(
        email=req.email,
        otp=req.otp,
        session_token_hash=session_token_hash,
    )

    with transaction() as tx:
        tx.execute(Q_MAYBE_SET_SESSION_TOKEN_HASH, params)
        row = tx.fetchone()
        if row:
            return dict(
                session_token=session_token,
                onboarded=row['person_id'] is not None,
            )
        else:
            return 'Invalid OTP', 401

def patch_onboardee_info(req: t.PatchOnboardeeInfo, s: t.SessionInfo):
    for field_name, field_value in req.dict().items():
        if field_value:
            break
    if not field_value:
        return f'No field set in {req.dict()}', 400

    if field_name in ['name', 'date_of_birth', 'about']:
        params = dict(
            email=s.email,
            field_value=field_value
        )

        q_set_onboardee_field = """
            INSERT INTO onboardee (
                email,
                $field_name
            ) VALUES (
                %(email)s,
                %(field_value)s
            ) ON CONFLICT (email) DO UPDATE SET
                $field_name = EXCLUDED.$field_name
            """.replace('$field_name', field_name)

        with transaction() as tx:
            tx.execute(q_set_onboardee_field, params)
    elif field_name == 'location':
        params = dict(
            email=s.email,
            friendly=field_value
        )

        q_set_onboardee_field = """
            INSERT INTO onboardee (
                email,
                location_id
            ) SELECT
                %(email)s,
                id
            FROM location
            WHERE friendly = %(friendly)s
            ON CONFLICT (email) DO UPDATE SET
                location_id = EXCLUDED.location_id
            """
        with transaction() as tx:
            tx.execute(q_set_onboardee_field, params)
    elif field_name == 'gender':
        params = dict(
            email=s.email,
            gender=field_value
        )

        q_set_onboardee_field = """
            INSERT INTO onboardee (
                email,
                gender_id
            ) SELECT
                %(email)s,
                id
            FROM gender
            WHERE name = %(gender)s
            ON CONFLICT (email) DO UPDATE SET
                gender_id = EXCLUDED.gender_id
            """

        with transaction() as tx:
            tx.execute(q_set_onboardee_field, params)
    elif field_name == 'other_peoples_genders':
        params = dict(
            email=s.email,
            genders=field_value
        )

        q_set_onboardee_field = """
            INSERT INTO onboardee_search_preference_gender (
                email,
                gender_id
            )
            SELECT
                %(email)s,
                id
            FROM gender
            WHERE name = ANY(%(genders)s)
            ON CONFLICT (email, gender_id) DO UPDATE SET
                gender_id = EXCLUDED.gender_id
            """

        with transaction() as tx:
            tx.execute(q_set_onboardee_field, params)
    elif field_name == 'files':
        pos_img_uuid = [
            (pos, img, secrets.token_hex(32))
            for pos, img in field_value.items()
        ]
        img_key = [
            (converted_img, key)
            for _, img, uuid in pos_img_uuid
            for converted_img, key in [
                (process_image(img, output_size=None), f'original-{uuid}.jpg'),
                (process_image(img, output_size=450), f'450-{uuid}.jpg'),
                (process_image(img, output_size=900), f'900-{uuid}.jpg')]
        ]

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(push_to_object_store, img, key)
                for img, key in img_key}

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print('Upload failed with exception:', e)
                    return '', 500

        params = [
            dict(email=s.email, position=pos, uuid=uuid)
            for pos, _, uuid in pos_img_uuid
        ]

        q_set_onboardee_field = """
            WITH
            previous_onboardee_photo AS (
                SELECT uuid
                FROM onboardee_photo
                WHERE
                    email = %(email)s AND
                    position = %(position)s
            )
            INSERT INTO onboardee_photo (
                email,
                position,
                uuid
            ) VALUES (
                %(email)s,
                %(position)s,
                %(uuid)s
            ) ON CONFLICT (email, position) DO UPDATE SET
                uuid = EXCLUDED.uuid
            RETURNING (SELECT uuid FROM previous_onboardee_photo);
            """

        with transaction() as tx:
            tx.executemany(q_set_onboardee_field, params, returning=True)
            previous_onboardee_photos = fetchall_sets(tx)

        delete_images_from_object_store(
            row['uuid'] for row in previous_onboardee_photos)
    else:
        return f'Invalid field name {field_name}', 400

def delete_onboardee_info(req: t.DeleteOnboardeeInfo, s: t.SessionInfo):
    params = [
        dict(email=s.email, position=position)
        for position in req.files
    ]

    with transaction() as tx:
        tx.executemany(Q_DELETE_ONBOARDEE_PHOTO, params, returning=True)
        previous_onboardee_photos = fetchall_sets(tx)

    delete_images_from_object_store(
        row['uuid'] for row in previous_onboardee_photos)

def post_complete_onboarding(s: t.SessionInfo):
    params = dict(email=s.email)

    with transaction() as tx:
        tx.execute(Q_COMPLETE_ONBOARDING_1, params)
        tx.execute(Q_COMPLETE_ONBOARDING_2, params)

def get_personality(person_id: int):
    params = dict(
        person_id=person_id,
    )

    with transaction('READ COMMITTED') as tx:
        return {
            row['trait']: row['percentage']
            for row in tx.execute(Q_SELECT_PERSONALITY, params).fetchall()
        }


# TODO
# with transaction() as tx:
#     tx.execute(
#         """
#         select
#             person_id,
#             question_id,
#             question,
#             answer
#         from answer
#         join question
#         on question_id = question.id
#         """,
#     )
# 
#     import json
#     j_str = json.dumps(tx.fetchall(), indent=2)
#     with open(
#             '/home/christian/duolicious-backend/answers.json',
#             'w',
#             encoding="utf-8"
#     ) as f:
#         f.write(j_str)
