from database import api_tx, chat_tx
from lxml import etree
import asyncio
import base64
import duohash
import regex
import websockets
import traceback

# TODO: Push notifications, yay
# TODO: async db ops
# TODO: Lock down the XMPP server by only allowing certain types of message

Q_UNIQUENESS = """
INSERT INTO intro_hash (hash)
VALUES (%(hash)s)
ON CONFLICT DO NOTHING
RETURNING hash
"""

Q_IS_SKIPPED = """
SELECT
    1
FROM
    skipped
WHERE
    (
        subject_person_id = %(from_username)s AND
        object_person_id  = %(to_username)s
    )
OR
    (
        subject_person_id = %(to_username)s AND
        object_person_id  = %(from_username)s
    )
LIMIT 1
"""

Q_SET_MESSAGED = """
INSERT INTO messaged (
    subject_person_id,
    object_person_id
) VALUES (
    %(subject_person_id)s,
    %(object_person_id)s
) ON CONFLICT DO NOTHING
"""

MAX_MESSAGE_LEN = 5000

NON_ALPHANUMERIC_RE = regex.compile(r'[^\p{L}\p{N}]')
REPEATED_CHARACTERS_RE = regex.compile(r'(.)\1{1,}')

class Username:
    def __init__(self):
        self.username = None

def parse_xml(s):
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    return etree.fromstring(s, parser=parser)

def get_message_attrs(message_xml):
    try:
        # Create a safe XML parser
        root = parse_xml(message_xml)

        if root.tag != '{jabber:client}message':
            raise Exception('Not a message')

        if root.attrib.get('type') != 'chat':
            raise Exception('type != chat')

        do_check_uniqueness = root.attrib.get('check_uniqueness') == 'true'

        maybe_message_body = root.find('{jabber:client}body')

        maybe_message_body = None
        body = root.find('{jabber:client}body')
        if body is not None:
            maybe_message_body = body.text

        return (
            root.attrib.get('id'),
            root.attrib.get('to'),
            do_check_uniqueness,
            maybe_message_body)
    except Exception as e:
        pass

    return None, None, None, None

def normalize_message(message_str):
    message_str = message_str.lower()

    # Remove everything but non-alphanumeric characters
    message_str = NON_ALPHANUMERIC_RE.sub('', message_str)

    # Remove repeated characters
    message_str = REPEATED_CHARACTERS_RE.sub(r'\1', message_str)

    return message_str

def is_message_too_long(message_str):
    return len(message_str) > MAX_MESSAGE_LEN

def is_message_unique(message_str):
    normalized = normalize_message(message_str)
    hashed = duohash.md5(normalized)

    params = dict(hash=hashed)

    with chat_tx('READ COMMITTED') as tx:
        if tx.execute(Q_UNIQUENESS, params).fetchall():
            return True
        else:
            return False

def is_message_blocked(username, to_jid):
    try:
        from_username = int(username)
        to_username = int(to_jid.split('@')[0])

        params = dict(
            from_username=from_username,
            to_username=to_username,
        )

        with api_tx('READ COMMITTED') as tx:
            return bool(tx.execute(Q_IS_SKIPPED, params).fetchall())
    except:
        print(traceback.format_exc())
        return True

    return False

def set_messaged(username, to_jid):
    from_username = int(username)
    to_username = int(to_jid.split('@')[0])

    params = dict(
        subject_person_id=from_username,
        object_person_id=to_username,
    )

    with api_tx('READ COMMITTED') as tx:
        tx.execute(Q_SET_MESSAGED, params)

def process_auth(message_str, username):
    if username.username is not None:
        return

    try:
        # Create a safe XML parser
        root = parse_xml(message_str)

        if root.tag != '{urn:ietf:params:xml:ns:xmpp-sasl}auth':
            raise Exception('Not an auth message')

        base64encoded = root.text
        decodedBytes = base64.b64decode(base64encoded)
        decodedString = decodedBytes.decode('utf-8')

        auth_parts = decodedString.split('\0')

        auth_username = auth_parts[1]

        username.username = auth_username
    except Exception as e:
        pass

def process_duo_message(message_xml, username):
    id, to_jid, do_check_uniqueness, maybe_message_body = get_message_attrs(
        message_xml)

    if id and maybe_message_body and is_message_too_long(maybe_message_body):
        return [f'<duo_message_too_long id="{id}"/>'], []

    if id and is_message_blocked(username, to_jid):
        return [f'<duo_message_blocked id="{id}"/>'], []

    if id and maybe_message_body and do_check_uniqueness and \
            not is_message_unique(maybe_message_body):
        return [f'<duo_message_not_unique id="{id}"/>'], []

    if id:
        set_messaged(username, to_jid)
        return (
            [
                f'<duo_message_delivered id="{id}"/>'
            ],
            [
                message_xml,
                f"<iq id='{duohash.duo_uuid()}' type='set'>"
                f"  <query"
                f"    xmlns='erlang-solutions.com:xmpp:inbox:0#conversation'"
                f"    jid='{to_jid}'"
                f"  >"
                f"    <box>chats</box>"
                f"  </query>"
                f"</iq>"

            ]
        )

    return [], [message_xml]

async def process(src, dst, username):
    async for message in src:
        process_auth(message, username)

        to_src, to_dst = process_duo_message(message, username.username)

        for m in to_dst:
            await dst.send(m)
        for m in to_src:
            await src.send(m)

async def forward(src, dst):
    async for message in src:
        await dst.send(message)

async def proxy(local_ws, path):
    username = Username()

    async with websockets.connect('ws://127.0.0.1:5442') as remote_ws:
        l2r_task = asyncio.ensure_future(process(local_ws, remote_ws, username))
        r2l_task = asyncio.ensure_future(forward(remote_ws, local_ws))

        done, pending = await asyncio.wait(
            [l2r_task, r2l_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

start_server = websockets.serve(proxy, '0.0.0.0', 5443, subprotocols=['xmpp'])

asyncio.get_event_loop().run_until_complete(start_server)
asyncio.get_event_loop().run_forever()
