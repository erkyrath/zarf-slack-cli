#!/usr/bin/env python3

### figure out how to display threading
### private chats and group chats. Should we be using conversations.list?
### mark channels that we're on! sort them to bottom
### on wake, rtm_read throws ConnectionResetError, but only after I try to send something. (ping?)
### /recap [CHAN] [N]
### /users [TEAM], /channels [TEAM]
### /users [CHAN]
### /reload TEAM (for users, channels)

# http://python-prompt-toolkit.readthedocs.io/en/master/pages/building_prompts.html
# https://github.com/ErikKalkoken/slackApiDoc

import sys
import os
import re
import time
import json
from collections import OrderedDict
import optparse
import asyncio
import threading
import prompt_toolkit
from ssl import SSLError
from slackclient import SlackClient

token_file = '.zlack-tokens'

popt = optparse.OptionParser(usage='slack-client.py [ OPTIONS ]')

(opts, args) = popt.parse_args()

thread = None
connections = OrderedDict()
debug_messages = False

def read_tokens():
    path = os.path.join(os.environ.get('HOME'), token_file)
    try:
        fl = open(path)
        dat = json.load(fl, object_pairs_hook=OrderedDict)
        fl.close()
        return dat
    except:
        return OrderedDict()

class ZarfSlackClient(SlackClient):
    def __init__(self, token, proxies=None, handler=None):
        SlackClient.__init__(self, token, proxies)
        self.server.websocket_safe_read = None
        self.message_handler = handler
        self.msg_counter = 0
        self.msg_in_flight = {}
        
    def api_call_check(self, method, **kwargs):
        res = self.api_call(method, **kwargs)
        if not res.get('ok'):
            msg = 'Slack error (%s): %s' % (method, res.get('error', '???'),)
            thread.add_output(msg)
            return None
        return res

    def rtm_disconnect(self):
        if self.server.websocket is not None:
            print('### closing websocket')
            self.server.websocket.send_close()
            self.server.websocket.close()
            self.server.websocket = None
        self.server.connected = False
        self.server.last_connected_at = 0

    def rtm_send_json(self, msg):
        if 'id' in msg and msg['id'] is None:
            self.msg_counter += 1
            msg['id'] = self.msg_counter
            self.msg_in_flight[msg['id']] = msg
        if debug_messages:
            thread.add_output('Sending: %s' % (msg,))
        self.server.send_to_websocket(msg)

    def rtm_complete_in_flight(self, val):
        if val in self.msg_in_flight:
            return self.msg_in_flight.pop(val)
        return None

    def rtm_read(self):
        if self.server.websocket is None:
            return
        try:
            dat = self.server.websocket.recv()
            msg = None
            try:
                msg = json.loads(dat)
            except:
                thread.add_output('Websocket error: non-json message: %s' % (dat,))
            if msg is not None:
                self.message_handler(msg)
        except SSLError as ex:
            if ex.errno == 2:
                return
            raise

class Channel:
    def __init__(self, conn, id, name, private=False):
        self.conn = conn
        self.id = id
        self.name = name
        self.private = private
        self.member = True ###

    def muted(self):
        return (self.id in self.conn.muted_channels)
    
class Connection:
    def __init__(self, id):
        self.id = id
        self.team = tokens[id]
        self.team_name = self.team['team_name']
        self.user_id = self.team['user_id']
        self.users = {}
        self.users_by_display_name = {}
        self.channels = {}
        self.muted_channels = set()
        self.lastchannel = None
        self.client = ZarfSlackClient(self.team['access_token'], handler=self.handle_message)

    def handle_message(self, msg):
        if debug_messages:
            thread.add_output('Received: %s' % (msg,))
        typ = msg.get('type')
        if typ is None and msg.get('reply_to'):
            origmsg = self.client.rtm_complete_in_flight(msg.get('reply_to'))
            if not origmsg:
                thread.add_output('Mismatched reply_to (id %d, msg %s)' % (msg.get('reply_to'), msg.get('text')))
                return
            teamid = self.id
            chanid = origmsg.get('channel', '')
            userid = origmsg.get('user', '')
            # Print our successful messages even on muted channels
            text = decode_message(teamid, msg.get('text'))
            val = '[%s/%s] %s: %s' % (team_name(teamid), channel_name(teamid, chanid), user_name(teamid, userid), text)
            thread.add_output(val)
            return
                
        if typ == 'message':
            teamid = self.id
            chanid = msg.get('channel', '')
            userid = msg.get('user', '')
            subtype = msg.get('subtype', '')
            if chanid in self.muted_channels:
                return
            if subtype == 'message_deleted':
                userid = msg.get('previous_message').get('user', '')
                oldtext = msg.get('previous_message').get('text')
                oldtext = decode_message(teamid, oldtext)
                val = '[%s/%s] DEL %s: %s' % (team_name(teamid), channel_name(teamid, chanid), user_name(teamid, userid), oldtext)
                thread.add_output(val)
                return
            if subtype == 'message_changed':
                oldtext = msg.get('previous_message').get('text')
                oldtext = decode_message(teamid, oldtext)
                userid = msg.get('message').get('user', '')
                newtext = msg.get('message').get('text')
                newtext = decode_message(teamid, newtext)
                text = oldtext + '\n -> ' + newtext
                val = '[%s/%s] EDIT %s: %s' % (team_name(teamid), channel_name(teamid, chanid), user_name(teamid, userid), text)
                thread.add_output(val)
                return
            text = decode_message(teamid, msg.get('text'))
            val = '[%s/%s] %s: %s' % (team_name(teamid), channel_name(teamid, chanid), user_name(teamid, userid), text)
            thread.add_output(val)
            return

def get_next_cursor(res):
    metadata = res.get('response_metadata')
    if not metadata:
        return None
    return metadata.get('next_cursor', None)
    
def connect_to_teams():
    for id in tokens.keys():
        conn = Connection(id)
        connections[id] = conn
        
    for conn in connections.values():
        thread.add_output('Fetching prefs from %s' % (conn.team_name,))
        res = conn.client.api_call_check('users.prefs.get')
        if res:
            prefs = res.get('prefs')
            mutels = prefs.get('muted_channels')
            if mutels:
                conn.muted_channels = set(mutels.split(','))
        
        thread.add_output('Fetching users from %s' % (conn.team_name,))
        cursor = None
        while True:
            if thread.check_shutdown():
                return
            res = conn.client.api_call_check('users.list', cursor=cursor)
            if not res:
                break
            for user in res.get('members'):
                userid = user['id']
                username = user['profile']['display_name']
                if not username:
                    username = user['name']    # legacy data field
                userrealname = user['profile']['real_name']
                conn.users[userid] = (username, userrealname)
                conn.users_by_display_name[username] = userid
            cursor = get_next_cursor(res)
            if not cursor:
                break
        #print(conn.users)

        thread.add_output('Fetching channels from %s' % (conn.team_name,))
        cursor = None
        while True:
            if thread.check_shutdown():
                return
            res = conn.client.api_call_check('channels.list', exclude_archived=True, exclude_members=True, cursor=cursor)
            if not res:
                break
            for chan in res.get('channels'):
                chanid = chan['id']
                channame = chan['name']
                conn.channels[chanid] = Channel(conn, chanid, channame, False)
            cursor = get_next_cursor(res)
            if not cursor:
                break
            
        thread.add_output('Fetching private channels from %s' % (conn.team_name,))
        cursor = None
        while True:
            if thread.check_shutdown():
                return
            res = conn.client.api_call_check('groups.list', exclude_archived=True, exclude_members=True, cursor=cursor)
            if not res:
                break
            for chan in res.get('groups'):
                chanid = chan['id']
                channame = chan['name']
                conn.channels[chanid] = Channel(conn, chanid, channame, True)
            cursor = get_next_cursor(res)
            if not cursor:
                break
            
        #print(conn.channels)

    for conn in connections.values():
        res = conn.client.rtm_connect(reconnect=True, with_team_state=False)
        ### if not res, close connection

def read_connections():
    for conn in connections.values():
        conn.client.rtm_read()
    
def disconnect_all_teams():
    for conn in connections.values():
        conn.client.rtm_disconnect()
    
class SlackThread(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self, name='slack-thread')
        self.input_list = []
        self.output_list = []
        self.want_shutdown = False
        self.lock = threading.Lock()
        self.cond = threading.Condition()
        
    def run(self):
        connect_to_teams()
        while not self.check_shutdown():
            ls = self.fetch_inputs()
            for (teamid, msg) in ls:
                conn = connections.get(teamid)
                if not conn:
                    self.add_output('Cannot send: %s not connected.' % (team_name(teamid),))
                else:
                    conn.client.rtm_send_json(msg)
            read_connections()
            with self.cond:
                self.cond.wait(0.1)
        disconnect_all_teams()
        self.add_output('Disconnected.')

    def set_shutdown(self):
        with self.cond:
            self.want_shutdown = True
            self.cond.notify()

    def check_shutdown(self):
        with self.lock:
            flag = self.want_shutdown
        return flag

    def add_input(self, val):
        with self.cond:
            self.input_list.append(val)
            self.cond.notify()
            
    def fetch_inputs(self):
        res = []
        with self.lock:
            if self.input_list:
                res.extend(self.input_list)
                self.input_list.clear()
        return res
    
    def add_output(self, val):
        with self.lock:
            self.output_list.append(val)
            
    def fetch_outputs(self):
        res = []
        with self.lock:
            if self.output_list:
                res.extend(self.output_list)
                self.output_list.clear()
        return res

# (teamid, channelid) for the current default channel
curchannel = None

pat_special_command = re.compile('/([a-z0-9_-]+)', flags=re.IGNORECASE)
pat_channel_command = re.compile('#([a-z0-9_-]+)(?:[/:]([a-z0-9_-]*))?', flags=re.IGNORECASE)

def handle_input(val):
    global curchannel
    match = pat_special_command.match(val)
    if match:
        cmd = match.group(1).lower()
        val = val[ match.end() : ]
        val = val.lstrip()
        if cmd == 'debug':
            cmd_debug(val)
        elif cmd == 'users':
            cmd_users(val)
        elif cmd == 'channels':
            cmd_channels(val)
        else:
            print('Special command not recognized:', cmd)
        return
    match = pat_channel_command.match(val)
    if match:
        val = val[ match.end() : ]
        val = val.lstrip()
        if match.group(2) is not None:
            # command "#TEAM/CHANNEL"
            team = parse_team(match.group(1))
            if not team:
                print('Team not recognized:', match.group(1))
                return
            teamid = team['team_id']
            channame = match.group(2)
        else:
            # command "#CHANNEL"
            if not curchannel:
                print('No current team.')
                return
            teamid = curchannel[0]
            channame = match.group(1)
        conn = connections.get(teamid)
        if not conn:
            print('Team not connected:', team_name(teamid))
            return
        chanid = parse_channel(conn, channame)
        if not chanid:
            print('Channel not recognized:', channame)
            return
        conn.lastchannel = chanid
        curchannel = (teamid, chanid)

    if val.startswith(';'):
        val = val[1:].lstrip()
    if not val:
        return
    if not curchannel:
        print('No current channel.')
        return
    
    (teamid, chanid) = curchannel
    team = tokens[teamid]
    text = encode_message(teamid, val)
    thread.add_input( (teamid, { 'type':'message', 'id':None, 'user':team['user_id'], 'channel':chanid, 'text':text }) )
    
def cmd_debug(args):
    global debug_messages
    debug_messages = not debug_messages
    print('Message debugging now %s' % (debug_messages,))

def cmd_users(args):
    if not curchannel:
        print('No current team.')
        return
    teamid = curchannel[0]
    conn = connections.get(teamid)
    if not conn:
        print('Team not connected:', team_name(teamid))
        return
    print(conn.users)

def cmd_channels(args):
    if not curchannel:
        print('No current team.')
        return
    teamid = curchannel[0]
    conn = connections.get(teamid)
    if not conn:
        print('Team not connected:', team_name(teamid))
        return
    ls = list(conn.channels.values())
    ls.sort(key=lambda chan:(chan.member, chan.muted(), chan.name))
    for chan in ls:
        idstring = (' (id %s)' % (chan.id,) if debug_messages else '')
        privflag = (' (priv)' if chan.private else '')
        muteflag = (' (mute)' if chan.muted() else '')
        print(' %s%s%s%s' % (chan.name, idstring, privflag, muteflag))
        
def parse_team(val):
    for team in tokens.values():
        if team.get('team_id') == val:
            return team
        if team.get('team_name').startswith(val):
            return team
        alias = team.get('alias')
        if alias and val in alias:
            return team
    return None

def parse_channel(conn, val):
    if not val:
        return conn.lastchannel
    for (id, chan) in conn.channels.items():
        if val == id or val == chan.name:
            return id
    for (id, chan) in conn.channels.items():
        if chan.name.startswith(val):
            return id
    return None

pat_user_id = re.compile('@([a-z0-9._]+)', flags=re.IGNORECASE)
pat_encoded_user_id = re.compile('<@([a-z0-9_]+)>', flags=re.IGNORECASE)

def decode_message(teamid, val):
    if val is None:
        return ''
    val = pat_encoded_user_id.sub(lambda match:'@'+user_name(teamid, match.group(1)), val)
    # We could translate <URL> and <URL|SLUG> here, but those look fine as is
    if '\n' in val:
        val = val.replace('\n', '\n... ')
    if '&' in val:
        val = val.replace('&lt;', '<')
        val = val.replace('&gt;', '>')
        val = val.replace('&amp;', '&')
    return val;

def encode_message(teamid, val):
    val = val.replace('<', '&lt;')
    val = val.replace('>', '&gt;')
    val = val.replace('&', '&amp;')
    val = pat_user_id.sub(lambda match:encode_exact_user_id(teamid, match), val)
    return val

def encode_exact_user_id(teamid, match):
    orig = match.group(0)
    val = match.group(1)
    conn = connections.get(teamid)
    if not conn:
        return orig
    if val not in conn.users_by_display_name:
        return orig
    return '<@' + conn.users_by_display_name[val] + '>'

def team_name(teamid):
    if teamid not in tokens:
        return '???'+teamid
    team = tokens[teamid]
    alias = team.get('alias')
    if alias:
        return alias[0]
    return team['team_name']

def channel_name(teamid, chanid):
    if teamid not in connections:
        return '???'+chanid
    conn = connections[teamid]
    if chanid not in conn.channels:
        return '???'+chanid
    return conn.channels[chanid].name

def user_name(teamid, userid):
    if teamid not in connections:
        return userid
    conn = connections[teamid]
    if userid not in conn.users:
        return userid
    return conn.users[userid][0]

async def input_loop():
    history = prompt_toolkit.history.InMemoryHistory()
    while thread.is_alive():
        try:
            prompt = '> '
            if curchannel:
                (teamid, chanid) = curchannel
                prompt = '%s/%s> ' % (team_name(teamid), channel_name(teamid, chanid))
            input = await prompt_toolkit.prompt_async(prompt, history=history, patch_stdout=True)
            input = input.rstrip()
            if input:
                handle_input(input)
        except KeyboardInterrupt:
            print('<KeyboardInterrupt>')
            thread.set_shutdown()
        except EOFError:
            print('<EOFError>')
            thread.set_shutdown()

def check_for_outputs(evloop):
    ls = thread.fetch_outputs();
    for ln in ls:
        print(ln)
    if thread.is_alive():
        evloop.call_later(0.1, check_for_outputs, evloop)

tokens = read_tokens()

thread = SlackThread()
thread.start()

evloop = asyncio.get_event_loop()

evloop.call_soon(check_for_outputs, evloop)

evloop.run_until_complete(input_loop())
evloop.close()
