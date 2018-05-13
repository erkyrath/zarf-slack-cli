#!/usr/bin/env python3

"""
zlack-client.py: A minimalist command-line Slack client.

To use this, you must first run the zlack-auth.py. This authorizes
you with Slack and writes your access token into ~/.zlack-tokens.

The structure of the client is awkward. The Slack connection runs in
its own background thread; it periodically checks the Slack websocket
connection (or connections) for updates.

The foreground thread runs the prompt_toolkit utility, which waits for
command-line input (with editing and command history). This is wrapped
in an asyncio wrapper. An async coroutine periodically pops up and
checks for any activity from the Slack thread.

(This would all be simpler if the slackclient library was written in
async style. Then I wouldn't need a second thread; everything could
just be async. Sadly, that's not what we've got.)

"""

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
from websocket._exceptions import WebSocketConnectionClosedException

token_file = '.zlack-tokens'

popt = optparse.OptionParser(usage='slack-client.py [ OPTIONS ]')

(opts, args) = popt.parse_args()

thread = None
teams = None
debug_messages = False

def read_teams():
    """Read the current token list from ~/.zlack-tokens.
    Return a dict of Team objects.
    """
    path = os.path.join(os.environ.get('HOME'), token_file)
    try:
        fl = open(path)
        dat = json.load(fl, object_pairs_hook=OrderedDict)
        fl.close()
    except:
        dat = OrderedDict()
    res = OrderedDict()
    for (id, map) in dat.items():
        res[id] = Team(map)
    return res

class ZarfSlackClient(SlackClient):
    """A customized version of SlackClient.
    This reworks the existing websocket-read mechanism entirely; I don't
    like the way slackclient does it. I also add a few more handy
    features.

    This runs in the background thread, so it should never print anything
    directly. All output is routed through the thread.add_output call.
    """
    def __init__(self, team, token, proxies=None, handler=None):
        SlackClient.__init__(self, token, proxies)
        self.teamref = team
        self.server.websocket_safe_read = None
        self.message_handler = handler
        self.last_pinged_at = None
        self.msg_counter = 0
        self.msg_in_flight = {}
        
    def api_call_check(self, method, **kwargs):
        """Make a web API call. Return the result.
        On error, print an error message and return None.
        """
        res = self.api_call(method, **kwargs)
        if not res.get('ok'):
            msg = 'Slack error (%s): %s' % (method, res.get('error', '???'),)
            thread.add_output(msg)
            return None
        return res

    def rtm_disconnect(self, goterror=False):
        """Disconnect the web socket. (The slackclient library doesn't
        have this for some reason.)
        Pass goterror=True if we're calling this because of a websocket
        error. In this case, we don't try to close the socket cleanly,
        we just throw it away.
        """
        if self.server.websocket is not None:
            if not goterror:
                self.server.websocket.send_close()
                self.server.websocket.close()
            self.server.websocket = None
        self.server.connected = False
        self.server.last_connected_at = 0
        self.last_pinged_at = None
        self.msg_in_flight.clear()

    def rtm_send_json(self, msg):
        """Send a message object to the server. The argument should
        be a JSONable dict.
        If msg.id is None, the value is replaced with a unique integer
        before sending. (This is handy for tracking the reply_to.)
        """
        if 'id' in msg and msg['id'] is None:
            self.msg_counter += 1
            msg['id'] = self.msg_counter
            self.msg_in_flight[msg['id']] = msg
        if debug_messages:
            thread.add_output('Sending (%s): %s' % (team_name(self.teamref), msg,))
        self.server.send_to_websocket(msg)

    def rtm_complete_in_flight(self, val):
        """Check an id value in a reply_to. If we've sent a message
        with that value, return it (and remove it from our pool of sent
        messages.)
        """
        if val in self.msg_in_flight:
            return self.msg_in_flight.pop(val)
        return None

    def rtm_read(self):
        """Read messages from the web socket until we don't see any more.
        Decode each one and pass it to our message_handler.

        This assumes that every distinct websocket message is a complete
        JSON object.

        Any exception (pretty much) means that the socket has died and
        should be reconnected. We can expect ConnectionResetError,
        WebSocketConnectionClosedException, and probably others.
        """
        if self.server.websocket is None or not self.server.connected:
            return
        now = time.time()
        if self.last_pinged_at and now > self.last_pinged_at + 5:
            self.last_pinged_at = now
            self.rtm_send_json({ 'type':'ping', 'id':None })
        while True:
            try:
                dat = self.server.websocket.recv()
                msg = None
                try:
                    msg = json.loads(dat)
                except:
                    thread.add_output('Websocket error: non-json message: %s' % (dat,))
                if msg is not None:
                    try:
                        self.message_handler(msg)
                    except Exception as ex:
                        thread.add_output('Error: %s: handling: %s' % (ex, msg,))
            except SSLError as ex:
                if ex.errno == 2:
                    # More data needs to be received on the underlying TCP
                    # transport before the request can be fulfilled.
                    # Exit quietly.
                    return
                # Other exceptions are passed along.
                raise

class Team:
    """A connection to one Slack group. This includes the websocket (which
    carries the RTM protocol). It also includes information about the
    group's channels and users.

    The information in this object is used by both the foreground and
    background threads, which is sloppy thread style. Sorry. I should
    do a lot more locking. (Or just rewrite the slack client library
    to be async!)
    """
    def __init__(self, map):
        self.id = map['team_id']
        self.team_name = map.get('team_name', '???')
        self.user_id = map['user_id']
        self.access_token = map['access_token']
        if 'alias' in map:
            self.alias = list(map['alias'])
        else:
            self.alias = []
        self.origmap = map

        self.users = {}
        self.users_by_display_name = {}
        self.channels = {}
        self.muted_channels = set()
        self.lastchannel = None
        self.client = ZarfSlackClient(self, self.access_token, handler=self.handle_message)

    def __repr__(self):
        return '<Team %s "%s">' % (self.id, self.team_name)

    def connected(self):
        """Return whether we have an active RTM connection to this
        group.
        """
        return self.client.server.connected
    
    def handle_message(self, msg):
        """Handle one RTM message, as received from the websocket connection.
        A message is a dict, as decoded from JSON.
        """
        if debug_messages:
            thread.add_output('Received (%s): %s' % (team_name(self), msg,))
            
        typ = msg.get('type')
        
        if typ is None and msg.get('reply_to'):
            # A reply to a message we sent.
            origmsg = self.client.rtm_complete_in_flight(msg.get('reply_to'))
            if not origmsg:
                thread.add_output('Mismatched reply_to (id %d, msg %s)' % (msg.get('reply_to'), msg.get('text')))
                return
            chanid = origmsg.get('channel', '')
            userid = origmsg.get('user', '')
            # Print our successful messages even on muted channels
            text = decode_message(self.id, msg.get('text'), msg.get('attachments'))
            val = '[%s/%s] %s: %s' % (team_name(self), channel_name(self, chanid), user_name(self, userid), text)
            thread.add_output(val)
            return

        if typ == 'hello':
            # Websocket-connected message. Start pinging.
            thread.add_output('<Connected: %s>' % (team_name(self)))
            self.client.last_pinged_at = time.time()
            return
                
        if typ == 'message':
            chanid = msg.get('channel', '')
            userid = msg.get('user', '')
            subtype = msg.get('subtype', '')
            if chanid in self.muted_channels:
                return
            if subtype == 'message_deleted':
                userid = msg.get('previous_message').get('user', '')
                oldtext = msg.get('previous_message').get('text')
                oldtext = decode_message(self.id, oldtext)
                val = '[%s/%s] (del) %s: %s' % (team_name(self), channel_name(self, chanid), user_name(self, userid), oldtext)
                thread.add_output(val)
                return
            if subtype == 'message_changed':
                oldtext = msg.get('previous_message').get('text')
                oldtext = decode_message(self.id, oldtext)
                userid = msg.get('message').get('user', '')
                newtext = msg.get('message').get('text')
                newtext = decode_message(self.id, newtext, msg.get('attachments'))
                if oldtext == newtext:
                    # Most likely this is a change to attachments, caused by Slack creating an image preview. Ignore.
                    return
                text = oldtext + '\n -> ' + newtext
                val = '[%s/%s] (edit) %s: %s' % (team_name(self), channel_name(self, chanid), user_name(self, userid), text)
                thread.add_output(val)
                return
            text = decode_message(self.id, msg.get('text'), msg.get('attachments'))
            subtypeflag = (' (%s)'%(subtype,) if subtype else '')
            val = '[%s/%s]%s %s: %s' % (team_name(self), channel_name(self, chanid), subtypeflag, user_name(self, userid), text)
            thread.add_output(val)
            return

class Channel:
    """Simple object representing one channel in a connection.
    """
    def __init__(self, team, id, name, private=False, member=True, im=None):
        self.team = team
        self.id = id
        self.name = name
        self.private = private
        self.member = member
        self.imuser = im

    def __repr__(self):
        if self.imuser:
            privflag = ' (im)'
        else:
            privflag = (' (priv)' if self.private else '')
        memberflag = (' (mem)' if self.member else '')
        return '<Channel %s%s%s: "%s">' % (self.id, privflag, memberflag, self.name)

    def muted(self):
        """Check whether this channel is muted. The mute flag is stored
        in the Team, because it comes from Slack's preferences data,
        not the channel data.
        """
        return (self.id in self.team.muted_channels)
    
class User:
    """Simple object representing one user in a connection.
    """
    def __init__(self, team, id, name, real_name):
        self.team = team
        self.id = id
        self.name = name
        self.real_name = real_name
        self.im_channel = None  # May be set later
        
    def __repr__(self):
        return '<User %s: "%s"/"%s">' % (self.id, self.name, self.real_name)
    
def connect_to_teams():
    for team in teams.values():
        thread.add_output('Fetching user information for %s' % (team.team_name,))
        # The muted_channels information is stored in your Slack preferences,
        # which are an undocumented (but I guess widely used) API call.
        # See: https://github.com/ErikKalkoken/slackApiDoc
        res = team.client.api_call_check('users.prefs.get')
        if res:
            prefs = res.get('prefs')
            mutels = prefs.get('muted_channels')
            if mutels:
                team.muted_channels = set(mutels.split(','))
        
        # Fetch user lists
        cursor = None
        while True:
            if thread.check_shutdown():
                return
            res = team.client.api_call_check('users.list', cursor=cursor)
            if not res:
                break
            for user in res.get('members'):
                userid = user['id']
                username = user['profile']['display_name']
                if not username:
                    username = user['name']    # legacy data field
                userrealname = user['profile']['real_name']
                team.users[userid] = User(team, userid, username, userrealname)
                team.users_by_display_name[username] = team.users[userid]
            cursor = get_next_cursor(res)
            if not cursor:
                break
        #print(team.users)

        # Fetch public and private channels
        cursor = None
        while True:
            if thread.check_shutdown():
                return
            res = team.client.api_call_check('conversations.list', exclude_archived=True, types='public_channel,private_channel', cursor=cursor)
            if not res:
                break
            for chan in res.get('channels'):
                chanid = chan['id']
                channame = chan['name']
                priv = chan['is_private']
                member = chan['is_member']
                team.channels[chanid] = Channel(team, chanid, channame, private=priv, member=member)
            cursor = get_next_cursor(res)
            if not cursor:
                break
            
        # Fetch IM (person-to-person) channels
        cursor = None
        while True:
            if thread.check_shutdown():
                return
            res = team.client.api_call_check('conversations.list', exclude_archived=True, types='im', cursor=cursor)
            if not res:
                break
            for chan in res.get('channels'):
                chanid = chan['id']
                chanuser = chan['user']
                if chanuser in team.users:
                    team.users[chanuser].im_channel = chanid
                    channame = '@'+team.users[chanuser].name
                    team.channels[chanid] = Channel(team, chanid, channame, private=True, member=True, im=chanuser)
            cursor = get_next_cursor(res)
            if not cursor:
                break
            
        #print(team.channels)

    # Now bring up all the RTM (websocket) connections.
    for team in teams.values():
        res = team.client.rtm_connect(auto_reconnect=True, with_team_state=False)
        ### if not res, close connection

def read_connections():
    """Check every active connection to see if messages have arrived from
    the Slack server. (Called on the Slack thread.)
    """
    for team in teams.values():
        try:
            if team.connected():
                team.client.rtm_read()
        except Exception as ex:
            thread.add_output('<Error: %s> %s' % (team_name(team.id), ex,))
            team.client.rtm_disconnect(goterror=True)
            team.client.server.rtm_connect(reconnect=True, use_rtm_start=False)

def disconnect_all_teams():
    """Disconnect all active connections.
    """
    for team in list(teams.values()):
        team.client.rtm_disconnect()
        ####
    
class SlackThread(threading.Thread):
    """Thread class which implements the background (Slack communications)
    thread.

    All message input and output is passed back and forth through the
    thread-safe add_input(), add_output() calls. Messages from the Slack
    server go to the output queue for printing. Messages from the user
    go to the input queue for transmission to Slack.
    """
    def __init__(self):
        threading.Thread.__init__(self, name='slack-thread')
        self.input_list = []
        self.output_list = []
        self.want_shutdown = False
        self.lock = threading.Lock()
        self.cond = threading.Condition()
        
    def run(self):
        """The body of the background thread.
        """
        # Start up, connect to all the Slack groups we're intersteed in.
        connect_to_teams()
        
        # The main loop: keep looping until someone sets our want_shutdown
        # flag.
        while not self.check_shutdown():
            # Check for messages from the user (the foreground thread).
            # Pass each one along to the Slack server.
            ls = self.fetch_inputs()
            for (teamid, msg) in ls:
                team = teams.get(teamid)
                if not (team and team.connected()):
                    self.add_output('Cannot send: %s not connected.' % (team_name(teamid),))
                else:
                    if callable(msg):
                        try:
                            msg(team)
                        except Exception as ex:
                            self.add_output('Exception: %s' % (ex,))
                    else:
                        team.client.rtm_send_json(msg)
            # Check for messages from the Slack server.
            read_connections()
            # Sleep 100 msec, or until the next add_input() call arrives.
            with self.cond:
                self.cond.wait(0.1)

        # We've been told to shut down.
        disconnect_all_teams()
        self.add_output('Disconnected.')

    def set_shutdown(self):
        """Set the want_shutdown flag. (thread-safe)
        """
        with self.cond:
            self.want_shutdown = True
            self.cond.notify()

    def check_shutdown(self):
        """Return the want_shutdown flag. (thread-safe)
        """
        with self.lock:
            flag = self.want_shutdown
        return flag

    def add_input(self, val):
        """Add a message to the input queue. (thread-safe)
        A message is a tuple (teamid, msg) which specifies a Slack
        group and a message to transmit there. The message should
        either be a JSONable dict, or a function to invoke on the
        background thread.
        When this called, we wake up the background thread to handle it,
        if it happens to be sleeping. 
        """
        with self.cond:
            self.input_list.append(val)
            self.cond.notify()
            
    def fetch_inputs(self):
        """Retrieve all queued messages from the input queue. (thread-safe)
        A message is a tuple (teamid, msg) as described above.
        """
        res = []
        with self.lock:
            if self.input_list:
                res.extend(self.input_list)
                self.input_list.clear()
        return res
    
    def add_output(self, val):
        """Add a message to the output queue (thread-safe).
        A message is just a string which will be printed at the console.
        """
        with self.lock:
            self.output_list.append(val)
            
    def fetch_outputs(self):
        """Retrieve all queued messages from the output queue. (thread-safe)
        """
        res = []
        with self.lock:
            if self.output_list:
                res.extend(self.output_list)
                self.output_list.clear()
        return res

# (teamid, channelid) for the current default channel
curchannel = None

pat_special_command = re.compile('/([a-z0-9_-]+)', flags=re.IGNORECASE)
pat_dest_command = re.compile('#([^ ]+)')

def handle_input(val):
    """Handle one input line from the player.
    This runs in the main thread.
    """
    global curchannel
    match = pat_special_command.match(val)
    if match:
        cmd = match.group(1).lower()
        val = val[ match.end() : ]
        val = val.lstrip()
        if cmd == 'help':
            cmd_help(val)
        elif cmd == 'debug':
            cmd_debug(val)
        elif cmd == 'teams':
            cmd_teams(val)
        elif cmd == 'connect':
            cmd_connect(val)
        elif cmd == 'disconnect':
            cmd_disconnect(val)
        elif cmd == 'reload':
            cmd_reload(val)
        elif cmd == 'users':
            cmd_users(val)
        elif cmd == 'channels':
            cmd_channels(val)
        elif cmd == 'recap':
            cmd_recap(val)
        else:
            print('Special command not recognized:', cmd)
        return

    match = pat_dest_command.match(val)
    if match:
        # The line starts with a channel prefix.
        cmd = match.group(1)
        val = val[ match.end() : ].lstrip()

        tup = parse_channelspec(cmd)
        if not tup:
            return
        (team, chanid) = tup
        # Set the current channel.
        team.lastchannel = chanid
        curchannel = (team.id, chanid)

    # I habitually type lines starting with semicolon. Strip that out.
    if val.startswith(';'):
        val = val[1:].lstrip()
    # If there's no line at all, this was just a channel prefix. Exit.
    if not val:
        return

    # Send a message to the current channel!
    if not curchannel:
        print('No current channel.')
        return
    (teamid, chanid) = curchannel
    team = teams[teamid]
    text = encode_message(teamid, val)
    thread.add_input( (teamid, { 'type':'message', 'id':None, 'user':team.user_id, 'channel':chanid, 'text':text }) )

# ----------------

# Handlers for all the special (slash) commands.

def cmd_help(args):
    print('/help -- this list')
    print('/teams -- list all teams you are authorized with')
    print('/connect [team] -- connect (or reconnect) to a team')
    print('/disconnect [team] -- disconnect from a team')
    print('/reload [team] -- reload users and channels for a team')
    print('/channels [team] -- list all channels in the current team or a named team')
    print('/users [team] -- list all users in the current team or a named team')
    print('/recap [channel] [minutes] -- recap an amount of time (default five minutes) on the current channel or a named channel')
    print('/debug [bool] -- set stream debugging on/off or toggle')
    
def cmd_debug(args):
    global debug_messages
    if not args:
        debug_messages = not debug_messages
    else:
        args = args.lower()
        debug_messages = args.startswith('1') or args.startswith('y') or args.startswith('t') or args=='on'
    print('Message debugging now %s' % (debug_messages,))

def cmd_teams(args):
    ls = list(teams.values())
    ls.sort(key = lambda team:team.team_name)
    for team in ls:
        teamname = team.team_name
        memflag = ('*' if team.connected() else ' ')
        idstring = (' (id %s)' % (team.id,) if debug_messages else '')
        aliases = team.alias
        if aliases:
            aliases = ', '.join(aliases)
            aliasstr = ' (%s)' % (aliases,)
        else:
            aliasstr = ''
        print(' %s%s%s%s' % (memflag, teamname, idstring, aliasstr))
    
def cmd_users(args):
    if not args:
        if not curchannel:
            print('No current team.')
            return
        team = teams.get(curchannel[0])
        if not team:
            print('Team not connected:', team_name(curchannel[0]))
            return
    else:
        team = parse_team(args)
        if not team:
            print('Team not recognized:', args)
            return
    ls = list(team.users.values())
    ls.sort(key = lambda user:user.name)
    for user in ls:
        idstring = (' (id %s)' % (user.id,) if debug_messages else '')
        print('  %s%s: %s' % (user.name, idstring, user.real_name))

def cmd_channels(args):
    if not args:
        if not curchannel:
            print('No current team.')
            return
        team = teams.get(curchannel[0])
        if not team:
            print('Team not connected:', team_name(curchannel[0]))
            return
    else:
        team = parse_team(args)
        if not team:
            print('Team not recognized:', args)
            return
    ls = list(team.channels.values())
    ls = [ chan for chan in ls if not chan.imuser ]
    ls.sort(key=lambda chan:(not chan.member, chan.muted(), chan.name))
    for chan in ls:
        idstring = (' (id %s)' % (chan.id,) if debug_messages else '')
        memflag = ('*' if chan.member else ' ')
        privflag = (' (priv)' if chan.private else '')
        muteflag = (' (mute)' if chan.muted() else '')
        print(' %s%s%s%s%s' % (memflag, chan.name, idstring, privflag, muteflag))

def cmd_connect(args):
    global curchannel
    if not args:
        if not curchannel:
            print('No current team.')
            return
        team = teams.get(curchannel[0])
        if not team:
            print('Team not recognized:', team_name(curchannel[0]))
            return
    else:
        team = parse_team(args)
        if not team:
            print('Team not recognized:', args)
            return
    if team.connected():
        # Should be thread-smarter about this!
        team.client.rtm_disconnect()
        if curchannel and curchannel[0] == team.id:
            curchannel = None
        print('Disconnected from', team_name(team))
    res = team.client.rtm_connect(auto_reconnect=True, with_team_state=False)
    ### if not res, close connection
    print('Connected to', team_name(team))

def cmd_disconnect(args):
    global curchannel
    if not args:
        if not curchannel:
            print('No current team.')
            return
        team = teams.get(curchannel[0])
        if not team:
            print('Team not recognized:', team_name(curchannel[0]))
            return
    else:
        team = parse_team(args)
        if not team:
            print('Team not recognized:', args)
            return
    if not team.connected():
        print('Team not connected:', team_name(team))
        return
    # Should be thread-smarter about this!
    team.client.rtm_disconnect()
    if curchannel and curchannel[0] == team.id:
        curchannel = None
    print('Disconnected from', team_name(team))

def cmd_reload(args):
    pass ###

def cmd_recap(args):
    args = args.split()
    if args and args[0].startswith('#'):
        arg = args.pop(0)
        tup = parse_channelspec(arg[1:])
        if not tup:
            return
        (team, chanid) = tup
    else:
        if not curchannel:
            print('No current team.')
            return
        (teamid, chanid) = curchannel
        team = teams.get(teamid)
        if not team:
            print('Team not connected:', team_name(teamid))
            return
    if not args:
        count = 5
    else:
        arg = args[0]
        try:
            if not arg:
                print('You must supply a number of minutes.')
                return
            count = int(arg)
            if count < 1:
                print('Recap must be a positive number of minutes.')
                return
        except:
            print('Not a number:', arg)
            return
        
    def func(team):
        timestamp = str(int(time.time()) - count*60)
        cursor = None
        while True:
            res = team.client.api_call_check('conversations.history', channel=chanid, oldest=timestamp, cursor=cursor)
            if not res:
                break
            for msg in reversed(res.get('messages')):
                userid = msg.get('user', '')
                subtype = msg.get('subtype', '')
                if subtype:
                    continue
                ts = msg.get('ts')
                ts = short_timestamp(ts)
                text = decode_message(team.id, msg.get('text'), msg.get('attachments'))
                val = '[%s/%s] (%s) %s: %s' % (team_name(team), channel_name(team, chanid), ts, user_name(team, userid), text)
                thread.add_output(val)
            cursor = get_next_cursor(res)
            if not cursor:
                break

    # Schedule the recap function on the Slack thread.
    thread.add_input( (team.id, func) )

# ----------------

# Utilities used by this and that.
    
def get_next_cursor(res):
    """Extract the next_cursor field from a message object. This is
    used by all Web API calls which get paginated results.
    """
    metadata = res.get('response_metadata')
    if not metadata:
        return None
    return metadata.get('next_cursor', None)
    
pat_channel_command = re.compile('^(?:([a-z0-9_-]+)[/:])?([a-z0-9_-]+)$', flags=re.IGNORECASE)
pat_im_command = re.compile('^(?:([a-z0-9_-]+)[/:])?@([a-z0-9._]+)$', flags=re.IGNORECASE)
pat_defaultchan_command = re.compile('^([a-z0-9_-]+)[/:]$', flags=re.IGNORECASE)

def parse_channelspec(val):
    """Parse a channel specification, in any of its various forms:
    TEAM/CHANNEL TEAM/@USER TEAM/ CHANNEL @USER
    (No initial hash character, please.)

    Returns (team, channelid). On error, prints a message and
    returns None.
    """
    match_chan = pat_channel_command.match(val)
    match_im = pat_im_command.match(val)
    match_def = pat_defaultchan_command.match(val)
    
    if match_chan:
        match = match_chan
        if match.group(1) is not None:
            # format: "TEAM/CHANNEL"
            team = parse_team(match.group(1))
            if not team:
                print('Team not recognized:', match.group(1))
                return
        else:
            # format: "CHANNEL"
            if not curchannel:
                print('No current team.')
                return
            team = teams.get(curchannel[0])
            if not team:
                print('Team not recognized:', curchannel[0])
                return
        channame = match.group(2)
        if not team:
            print('Team not connected:', team_name(team))
            return
        chanid = parse_channel(team, channame)
        if not chanid:
            print('Channel not recognized:', channame)
            return
    elif match_im:
        match = match_im
        if match.group(1) is not None:
            # format: "TEAM/@USER"
            team = parse_team(match.group(1))
            if not team:
                print('Team not recognized:', match.group(1))
                return
        else:
            # format: "@USER"
            if not curchannel:
                print('No current team.')
                return
            team = teams.get(curchannel[0])
            if not team:
                print('Team not recognized:', curchannel[0])
                return
        username = match.group(2)
        if username not in team.users_by_display_name:
            print('User not recognized:', username)
            return
        chanid = team.users_by_display_name[username].im_channel
        if not chanid:
            print('No IM channel with user:', username)
            return
    elif match_def:
        match = match_def
        # format: "TEAM/"
        team = parse_team(match.group(1))
        if not team:
            print('Team not recognized:', match.group(1))
            return
        chanid = parse_channel(team, None)
        if not chanid:
            print('No default channel for team:', team_name(team))
            return
    else:
        print('Channel spec not recognized:', val)
        return

    return (team, chanid)

def parse_team(val):
    """Parse a team name, ID, or alias. Return the Team entry.
    """
    for team in teams.values():
        if team.id == val:
            return team
        if team.team_name.startswith(val):
            return team
        aliases = team.alias
        if aliases and val in aliases:
            return team
    return None

def parse_channel(team, val):
    """Parse a channel name (a bare channel, no # or team prefix)
    for a given Team. Return the channel ID.
    """
    if not val:
        return team.lastchannel
    for (id, chan) in team.channels.items():
        if val == id or val == chan.name:
            return id
    for (id, chan) in team.channels.items():
        if chan.name.startswith(val):
            return id
    return None

pat_user_id = re.compile('@([a-z0-9._]+)', flags=re.IGNORECASE)
pat_encoded_user_id = re.compile('<@([a-z0-9_]+)>', flags=re.IGNORECASE)

def decode_message(teamid, val, attachments=None):
    """Convert a plain-text message in standard Slack form into a printable
    string. You can also pass a list of attachments from the message.
    Slack message text has a few special features:
    - User references look like <@USERID>
    - URLs look like <URL> or <URL|SLUG>
    - &, <, and > characters are &-encoded (as in HTML)
    """
    if val is None:
        val = ''
    else:
        val = pat_encoded_user_id.sub(lambda match:'@'+user_name(teamid, match.group(1)), val)
        # We could translate <URL> and <URL|SLUG> here, but those look fine as is
        if '\n' in val:
            val = val.replace('\n', '\n... ')
        if '&' in val:
            val = val.replace('&lt;', '<')
            val = val.replace('&gt;', '>')
            val = val.replace('&amp;', '&')
    if attachments:
        for att in attachments:
            fallback = att.get('fallback')
            if fallback:
                if '\n' in fallback:
                    fallback = fallback.replace('\n', '\n... ')
                ### & < > also?
                val += ('\n..> ' + fallback)
    return val;

def encode_message(teamid, val):
    """Encode a human-typed message into standard Slack form.
    """
    val = val.replace('<', '&lt;')
    val = val.replace('>', '&gt;')
    val = val.replace('&', '&amp;')
    # We try to locate @displayname references and convert them to
    # <@USERID>.
    val = pat_user_id.sub(lambda match:encode_exact_user_id(teamid, match), val)
    return val

def encode_exact_user_id(teamid, match):
    """Utility function used by encode_message. Given a match object from
    pat_user_id, return a <@USERID> substitution. If the match doesn't
    exactly match a user display name, we return the original string.    
    """
    orig = match.group(0)  # '@name'
    val = match.group(1)   # 'name'
    team = teams.get(teamid)
    if not team:
        return orig
    if val not in team.users_by_display_name:
        return orig
    return '<@' + team.users_by_display_name[val].id + '>'

def short_timestamp(ts):
    """Given a Slack-style timestamp (a string like "1526150036.000002"),
    display it in a nice way.
    """
    tup = time.localtime(float(ts))
    # If the timestamp is from today, we use a shorter form.
    nowtup = time.localtime()
    if tup.tm_year == nowtup.tm_year and tup.tm_yday == nowtup.tm_yday:
        val = time.strftime('%H:%M', tup)
    else:
        val = time.strftime('%m/%d %H:%M', tup)
    return val

def team_name(team):
    """Look up a team name, either as an alias (if available) or the
    full name. The argument can be a Team or team ID string.
    """
    if not isinstance(team, Team):
        if team not in teams:
            return '???'+team
        team = teams[team]
    aliases = team.alias
    if aliases:
        return aliases[0]
    return team.team_name

def channel_name(team, chanid):
    """Look up a channel name.
    """
    if not isinstance(team, Team):
        if team not in teams:
            return '???'+chanid
        team = teams[team]
    if chanid not in team.channels:
        return '???'+chanid
    return team.channels[chanid].name

def user_name(team, userid):
    """Look up a user name (the displayname).
    """
    if not isinstance(team, Team):
        if team not in teams:
            return userid
        team = teams[team]
    if userid not in team.users:
        return userid
    return team.users[userid].name

async def input_loop():
    """This coroutine handles the "user interface" of the application.
    It runs in the main thread. Since it's async, it permits other
    eventloop items to execute in the main thread -- specifically,
    the check_for_outputs call.
    """
    # Create a history storage object for the command-line prompt.
    history = prompt_toolkit.history.InMemoryHistory()
    # Keep running as long as the Slack thread is running.
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
    """Check for output messages from the Slack thread. If there are
    any, print them.
    This runs in the main thread, every 100 milliseconds, as
    long as the eventloop is running.
    """
    ls = thread.fetch_outputs()
    for ln in ls:
        print(ln)
    if thread.is_alive():
        # Reschedule ourself to be called 100 milliseconds from now.
        evloop.call_later(0.1, check_for_outputs, evloop)

# Begin work.

teams = read_teams()
if not teams:
    print('You are not authorized in any Slack groups. Run zlack-auth.py first.')
    sys.exit(-1)

# Launch the Slack thread.
thread = SlackThread()
thread.start()

# We (the main thread) will be running a standard eventloop.

evloop = asyncio.get_event_loop()

# Set up the first call of the output checker. (It will re-invoke itself
# every 100 milliseconds.)
evloop.call_soon(check_for_outputs, evloop)

# Set up the main UI loop, and run it until it's done.
evloop.run_until_complete(input_loop())
evloop.close()
