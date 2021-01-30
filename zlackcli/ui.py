import re
import time
import os.path
import tempfile
import urllib.parse
import asyncio
import subprocess

pat_special_command = re.compile('/([a-z0-9?_-]+)', flags=re.IGNORECASE)
pat_dest_command = re.compile('#([^ ]+)')

pat_integer = re.compile('[0-9]+')
pat_url = re.compile('http[s]?:.*', flags=re.IGNORECASE)

pat_encoded_user_id = re.compile('<@([a-z0-9_]+)>', flags=re.IGNORECASE)
pat_encoded_channel_id = re.compile('<#([a-z0-9_]+)([|][a-z0-9_-]*)?>', flags=re.IGNORECASE)

pat_channel_command = re.compile('^(?:([a-z0-9_-]+)[/:])?([a-z0-9_-]+)$', flags=re.IGNORECASE)
pat_im_command = re.compile('^(?:([a-z0-9_-]+)[/:])?@([a-z0-9._]+)$', flags=re.IGNORECASE)
pat_defaultchan_command = re.compile('^([a-z0-9_-]+)[/:]$', flags=re.IGNORECASE)

class ArgException(Exception):
    """ArgException: Raised whenever a user command doesn't conform to the
    command syntax.
    """
    pass

class UICommand:
    """UICommand: Represents a user (slash) command. These objects
    are the result of the @uicommand decorator.
    """
    def __init__(self, name, aliases, isasync, help, arghelp, func):
        self.name = name
        self.aliases = set(aliases)
        self.isasync = isasync
        self.help = help
        self.arghelp = arghelp
        self.func = func
            
def uicommand(name, *aliases, isasync=False, help='???', arghelp=None):
    """The @uicommand decorator appears on UI methods which implement
    user (slash) commands.
    """
    return lambda func: UICommand(name, aliases, isasync, help, arghelp, func)

class UI:
    """UI: This object handles the user-interface side of the client.
    When the user types a command, it is handed to the handle_input()
    method; when a message arrives from Slack, it is handed to the
    handle_message() method. The class deals with all the input parsing
    and output formatting.
    """
    def __init__(self, client, opts=None):
        self.client = client

        self.curchannel = None
        self.lastchannel = None
        self.presumedchannel = None
        self.debug_messages = False

        if opts and opts.debug_messages:
            self.debug_messages = True

        self.file_counter = 0
        # Both of these map to (index, teamkey, url) tuples.
        self.files_by_index = {}
        self.files_by_url = {}

        tup = self.client.prefs.get('curchannel', None)
        if tup:
            self.curchannel = tuple(tup)

    def print(self, msg):
        """Output a line of text. (Or several lines, as it could contain
        internal line breaks.)  You typically won't want to customize this;
        instead, replace the Client.print() method.
        """
        self.client.print(msg)

    def print_exception(self, ex, label='zlack'):
        """Convenience function to print an exception using self.print().
        If ex is None, this does nothing (so you can conveniently use it
        when you only *might* have an exception). If --debugexceptions is
        set, this prints complete stack traces.
        """
        self.client.print_exception(ex, label)
        
    def display_current_channel(self):
        """Return a human-readable string representing the current channel.
        This is used as the input prompt.
        """
        if self.curchannel:
            (teamkey, chanid) = self.curchannel
            return '%s/%s' % (self.team_name(teamkey), self.channel_name(teamkey, chanid))
        return ''

    def display_rprompt(self, session):
        """Return the text to display on the right-hand side of the
        input line. We use this to display a warning flag if we think
        the user is mischannelling.
        """
        curtext = session.default_buffer.text
        # The presumedchannel is the last channel seen when the user started
        # typing. This is the one we assume they're replying to.
        if not curtext:
            self.presumedchannel = None
            return ''
        if self.presumedchannel is None:
            self.presumedchannel = self.lastchannel
        if curtext.startswith('#'):
            # Explicit channel destination, so no need for a warning.
            return ''
        if self.curchannel != self.presumedchannel:
            if self.presumedchannel is None:
                return '*?'
            else:
                (teamkey, chanid) = self.presumedchannel
                return '*%s?' % (self.channel_name(teamkey, chanid),)

    def note_send_message(self, msg, team):
        """Display a raw message if debugging is on.
        """
        if self.debug_messages:
            self.print('Sent (%s): %s' % (self.team_name(team), msg,))
        
    def note_receive_message(self, msg, team):
        """Display a raw message if debugging is on.
        """
        if self.debug_messages:
            self.print('Received (%s): %s' % (self.team_name(team), msg,))

    def note_file_urls(self, team, files):
        """Record URLs if they are not yet known.
        """
        for fil in files:
            url = fil.get('url_private')
            if url not in self.files_by_url:
                self.file_counter += 1
                tup = (self.file_counter, team.key, url)
                self.files_by_url[url] = tup
                self.files_by_index[self.file_counter] = tup
        
    def handle_message(self, msg, team):
        """Handle one message received from the Slack server (over the
        RTM websocket).
        """
        typ = msg.get('type')

        files = msg.get('files')
        if files:
            self.note_file_urls(team, files)

        if typ is None and msg.get('reply_to'):
            # A reply to a message we sent.
            origmsg = team.resolve_in_flight(msg.get('reply_to'))
            if not origmsg:
                self.print('Mismatched reply_to (id %d, msg %s)' % (msg.get('reply_to'), msg.get('text')))
                return
            chanid = origmsg.get('channel', '')
            userid = origmsg.get('user', '')
            # Print our successful messages even on muted channels
            text = self.decode_message(team, msg.get('text'), attachments=msg.get('attachments'), files=msg.get('files'))
            val = '[%s/%s] %s: %s' % (self.team_name(team), self.channel_name(team, chanid), self.user_name(team, userid), text)
            self.print(val)
            return
        
        if typ == 'hello':
            # Websocket-connected message.
            self.print('<Connected: %s>' % (self.team_name(team)))
            return
        
        if typ == 'message':
            chanid = msg.get('channel', '')
            userid = msg.get('user', '')
            subtype = msg.get('subtype', '')
            if chanid in team.muted_channels:
                return
            if subtype == 'message_deleted':
                userid = msg.get('previous_message').get('user', '')
                oldtext = msg.get('previous_message').get('text')
                oldtext = self.decode_message(team, oldtext)
                val = '[%s/%s] (del) %s: %s' % (self.team_name(team), self.channel_name(team, chanid), self.user_name(team, userid), oldtext)
                self.print(val)
                return
            if subtype == 'message_changed':
                oldtext = ''
                if 'previous_message' in msg:
                    oldtext = msg.get('previous_message').get('text')
                    oldtext = self.decode_message(team, oldtext)
                userid = msg.get('message').get('user', '')
                newtext = msg.get('message').get('text')
                newtext = self.decode_message(team, newtext, attachments=msg.get('attachments'), files=msg.get('files'))
                if oldtext == newtext:
                    # Most likely this is a change to attachments, caused by Slack creating an image preview. Ignore.
                    return
                text = oldtext + '\n -> ' + newtext
                val = '[%s/%s] (edit) %s: %s' % (self.team_name(team), self.channel_name(team, chanid), self.user_name(team, userid), text)
                self.print(val)
                self.lastchannel = (team.key, chanid)
                return
            if subtype == 'slackbot_response':
                val = self.client.prefs.tree_get('slackbot_mute', team, chanid)
                if val:
                    return
            text = self.decode_message(team, msg.get('text'), attachments=msg.get('attachments'), files=msg.get('files'))
            subtypeflag = (' (%s)'%(subtype,) if subtype else '')
            colon = (':' if subtype != 'me_message' else '')
            val = '[%s/%s]%s %s%s %s' % (self.team_name(team), self.channel_name(team, chanid), subtypeflag, self.user_name(team, userid), colon, text)
            self.print(val)
            self.lastchannel = (team.key, chanid)
            return

    def handle_input(self, val):
        """Handle one input line from the player.
        """
        match = pat_special_command.match(val)
        if match:
            cmd = match.group(1).lower()
            args = val[ match.end() : ].split()
            han = self.handler_map.get(cmd)
            if not han:
                self.print('Command not recognized: /%s' % (cmd,))
                return
            if not han.isasync:
                try:
                    han.func(self, args)
                except ArgException as ex:
                    self.print('Command /%s: %s' % (cmd, ex,))
                    return
                except Exception as ex:
                    self.print_exception(ex, 'Command /%s' % (cmd,))
            else:
                task = self.client.evloop.create_task(han.func(self, args))
                def callback(future):
                    ex = future.exception()
                    if ex and isinstance(ex, ArgException):
                        self.print('Command /%s: %s' % (cmd, ex,))
                    elif ex:
                        self.print_exception(ex, 'Command /%s' % (cmd,))
                task.add_done_callback(callback)
            return

        match = pat_dest_command.match(val)
        if match:
            # The line starts with a channel prefix.
            cmd = match.group(1)
            val = val[ match.end() : ].lstrip()

            try:
                tup = self.parse_channelspec(cmd)
            except ArgException as ex:
                self.print(ex)
                return
            (team, chanid) = tup
            # Set the current channel.
            team.lastchannel = chanid
            self.curchannel = (team.key, chanid)
            self.lastchannel = self.curchannel
            self.client.prefs.put('curchannel', self.curchannel)
    
        # I habitually type lines starting with semicolon. Strip that out.
        if val.startswith(';'):
            val = val[1:].lstrip()
            # Special case: a lone semicolon means "set to the last channel
            # we saw."
            if not val:
                if not self.lastchannel:
                    self.print('No recent channel.')
                else:
                    self.curchannel = self.lastchannel
                    self.client.prefs.put('curchannel', self.curchannel)
                return
            
        # TODO: A line starting with colon should generate a me_message.
        # However, I don't seem to be able to send me_message -- that subtype
        # is ignored. Maybe it needs to go via the web API?
        
        # If there's no line at all, this was just a channel prefix. Exit.
        if not val:
            return

        # Send a message to the current channel!
        if not self.curchannel:
            self.print('No current channel.')
            return
        (teamkey, chanid) = self.curchannel
        team = self.client.teams[teamkey]
        team.protocol.protoui.send_message(val, team=team, chanid=chanid)
                
    def decode_message(self, team, val, attachments=None, files=None):
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
            val = pat_encoded_user_id.sub(lambda match:'@'+self.user_name(team, match.group(1)), val)
            val = pat_encoded_channel_id.sub(lambda match:'#'+self.channel_name(team, match.group(1))+(match.group(2) if match.group(2) else ''), val)
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
        if files:
            for fil in files:
                url = fil.get('url_private')
                tup = self.files_by_url.get(url, None)
                index = tup[0] if tup else '?'
                val += ('\n..file [%s] %s (%s, %s bytes): %s' % (index, fil.get('title'), fil.get('pretty_type'), fil.get('size'), url, ))
        return val
    
    def short_timestamp(self, ts):
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

    def team_name(self, team):
        """Look up a team name, either as an alias (if available) or the
        full name. The argument can be a Host or team key string.
        """
        if team is None:
            return '<no team>'
        if not isinstance(team, Host):
            if team not in self.client.teams:
                return '???%s' % (team,)
            team = self.client.teams[team]
        aliases = team.get_aliases()
        if aliases:
            return aliases[0]
        return team.team_name
    
    def channel_name(self, team, chanid):
        """Look up a channel name.
        ### generic?
        """
        if not isinstance(team, Host):
            if team not in self.client.teams:
                return '???%s' % (chanid,)
            team = self.client.teams[team]
        if chanid not in team.channels:
            return '???%s' % (chanid,)
        return team.channels[chanid].name
    
    def user_name(self, team, userid):
        """Look up a user name (the displayname).
        ### probably not generic
        """
        if not isinstance(team, Host):
            if team not in self.client.teams:
                return userid
            team = self.client.teams[team]
        if userid not in team.users:
            return userid
        return team.users[userid].name

    def parse_channelspec(self, val):
        """Parse a channel specification, in any of its various forms:
        TEAM/CHANNEL TEAM/@USER TEAM/ CHANNEL @USER
        (No initial hash character, please.)
    
        Returns (team, channelid).
        """
        match_chan = pat_channel_command.match(val)
        match_im = pat_im_command.match(val)
        match_def = pat_defaultchan_command.match(val)
        
        if match_chan:
            match = match_chan
            knownteam = False
            if match.group(1) is not None:
                # format: "TEAM/CHANNEL"
                team = self.parse_team(match.group(1))
                knownteam = True
            else:
                # format: "CHANNEL"
                if not self.curchannel:
                    raise ArgException('No current team.')
                team = self.client.get_team(self.curchannel[0])
                if not team:
                    raise ArgException('Host not recognized: %s' % (self.curchannel[0],))
            channame = match.group(2)
            try:
                chanid = self.parse_channel(team, channame)
            except ArgException:
                if not knownteam:
                    (team, chanid) = self.parse_channel_anyteam(channame)
                else:
                    raise
        elif match_im:
            match = match_im
            if match.group(1) is not None:
                # format: "TEAM/@USER"
                team = self.parse_team(match.group(1))
            else:
                # format: "@USER"
                if not self.curchannel:
                    raise ArgException('No current team.')
                team = self.client.get_team(self.curchannel[0])
                if not team:
                    raise ArgException('Host not recognized: %s' % (self.curchannel[0],))
            username = match.group(2)
            if username not in team.users_by_display_name:
                raise ArgException('User not recognized: %s' % (username,))
            chanid = team.users_by_display_name[username].im_channel
            if not chanid:
                raise ArgException('No IM channel with user: %s' % (username,))
        elif match_def:
            match = match_def
            # format: "TEAM/"
            team = self.parse_team(match.group(1))
            chanid = team.lastchannel
            if not chanid:
                raise ArgException('No default channel for team: %s' % (self.team_name(team),))
        else:
            raise ArgException('Channel spec not recognized: %s' % (val,))
    
        return (team, chanid)

    def parse_team(self, val):
        """Parse a team name, ID, or alias. Returns the Host entry.
        Raises ArgException if not recognized.
        """
        for team in self.client.teams.values():
            if team.id == val:
                return team
            if team.team_name.startswith(val):
                return team
            aliases = team.get_aliases()
            if aliases and val in aliases:
                return team
        raise ArgException('Host not recognized: %s' % (val,))
    
    def parse_channel(self, team, val):
        """Parse a channel name (a bare channel, no # or team prefix)
        for a given Host. Returns the channel ID.
        Raises ArgException if not recognized.
        """
        for (id, chan) in team.channels.items():
            if val == id or val == chan.name:
                return id
        for (id, chan) in team.channels.items():
            if chan.name.startswith(val):
                return id
        raise ArgException('Channel not recognized: %s/%s' % (self.team_name(team), val,))
    
    def parse_channel_anyteam(self, val):
        """Parse a channel name, checking all teams.
        Returns (team, chanid).
        """
        for team in self.client.teams.values():
            for (id, chan) in team.channels.items():
                if val == id or val == chan.name:
                    return (team, id)
            for (id, chan) in team.channels.items():
                if chan.name.startswith(val):
                    return (team, id)
        raise ArgException('Channel not recognized: %s' % (val,))

    def parse_interval(self, val):
        """Convert a string to a number of seconds. This accepts values like
        "5" (default minutes), "10m", "2h", "1d".
        """
        pat_interval = re.compile('^([0-9]+)([a-z]*)$', flags=re.IGNORECASE)
        match = pat_interval.match(val)
        if not match:
            raise ArgException('Interval not recognized: %s' % (val,))
        try:
            count = int(match.group(1))
        except:
            raise ArgException('Interval has no number: %s' % (val,))
        unit = match.group(2).lower()
        if not unit:
            return count * 60  # minutes
        if unit in ('s', 'sec'):
            return count
        if unit in ('m', 'min'):
            return count * 60  # minutes
        if unit in ('h', 'hr', 'hour'):
            return count * 60 * 60  # hours
        if unit in ('d', 'day'):
            return count * 60 * 60 * 24  # days
        raise ArgException('Interval unit not recognized: %s' % (unit,))
    
    def parse_bool(self, val):
        """Convert a string to a boolean. Recognizes values like "true",
        "yes", "1", "on".
        """
        val = val.lower()
        if val.startswith('1') or val.startswith('y') or val.startswith('t') or val=='on':
            return True
        if val.startswith('0') or val.startswith('n') or val.startswith('f') or val=='off':
            return False
        raise ArgException('Boolean argument expected')

    def parse_team_or_current(self, args):
        """Parse an argument list of the form: "[team]"
        (Optional team name, or if none given, return the current
        team.)
        """
        if not args:
            if not self.curchannel:
                raise ArgException('No current team.')
            team = self.client.get_team(self.curchannel[0])
            if not team:
                raise ArgException('Host not recognized: %s' % (self.curchannel[0],))
        elif len(args) == 1:
            team = self.parse_team(args[0])
        else:
            raise ArgException('Expected zero or one arguments')
        return team

    # The user (slash) commands begin here. Each uses the @uicommand
    # decorator, and also must be listed in the handler_list array.
    
    @uicommand('help', '?',
               help='this list')
    def cmd_help(self, args):
        """Command: display the command list.
        """
        for han in self.handler_list:
            prefix = ''
            if han.arghelp:
                prefix = ' ' + han.arghelp
            self.print('/%s%s -- %s' % (han.name, prefix, han.help),)

    @uicommand('auth',
               help='request authentication to a Slack team')
    def cmd_auth(self, args):
        """Command: authenticate to a Slack team.
        """
        pro = self.client.protocolmap['slack'] ###
        pro.begin_auth()
        
    @uicommand('debug',
               arghelp='[bool]',
               help='set stream debugging on/off or toggle')
    def cmd_debug(self, args):
        """Command: set or toggle the debug_messages flag, which shows all
        commands that go to or from the Slack server.
        """
        if not args:
            self.debug_messages = not self.debug_messages
        elif len(args) == 1:
            self.debug_messages = self.parse_bool(args[0])
        else:
            raise ArgException('Expected zero or one arguments')
        self.print('Message debugging now %s' % (self.debug_messages,))

    @uicommand('connect',
               arghelp='[team]',
               help='connect (or reconnect) to a team')
    def cmd_connect(self, args):
        """Command: connect to a group. If we're already connected, disconnect
        and then reconnect.
        """
        team = self.parse_team_or_current(args)
        team.rtm_connect()

    @uicommand('disconnect',
               arghelp='[team]',
               help='disconnect from a team')
    def cmd_disconnect(self, args):
        """Command: disconnect from a group. This only applies to the RTM
        connection.
        """
        team = self.parse_team_or_current(args)
        if not team.rtm_connected():
            self.print('Host not connected: %s' % (self.team_name(team),))
            return
        team.rtm_disconnect()
        if self.curchannel and self.curchannel[0] == team.key:
            self.curchannel = None
            self.client.prefs.put('curchannel', self.curchannel)

    @uicommand('teams',
               help='list all teams you are authorized with')
    def cmd_teams(self, args):
        """Command: display the list of teams. Asterisk indicates an active
        RTM connection.
        """
        if args:
            raise ArgException('Expected no arguments')
        livecount = len([ pro for pro in self.client.protocols if pro.teams ])
        for pro in self.client.protocols:
            if livecount > 1:
                print('%s:' % (pro.key,))
            ls = list(pro.teams.values())
            ls.sort(key = lambda team:team.team_name)
            for team in ls:
                teamname = team.team_name
                memflag = ('*' if team.rtm_connected() else ' ')
                idstring = (' (id %s)' % (team.id,) if self.debug_messages else '')
                aliases = team.get_aliases()
                if aliases:
                    aliases = ', '.join(aliases)
                    aliasstr = ' (%s)' % (aliases,)
                else:
                    aliasstr = ''
                self.print(' %s%s%s%s' % (memflag, teamname, idstring, aliasstr))
    
    @uicommand('users',
               arghelp='[team]',
               help='list all users in the current team or a named team')
    def cmd_users(self, args):
        """Command: display the list of users.
        """
        team = self.parse_team_or_current(args)
        ls = list(team.users.values())
        ls.sort(key = lambda user:user.name)
        for user in ls:
            idstring = (' (id %s)' % (user.id,) if self.debug_messages else '')
            self.print('  %s%s: %s' % (user.name, idstring, user.real_name))
    
    @uicommand('channels',
               arghelp='[team]',
               help='list all channels in the current team or a named team')
    def cmd_channels(self, args):
        """Command: display the list of channels. Asterisk marks channels
        that we are members of. Muted and private channels are also flagged.
        """
        team = self.parse_team_or_current(args)
        ls = list(team.channels.values())
        ls = [ chan for chan in ls if not chan.imuser ]
        ls.sort(key=lambda chan:(not chan.member, chan.muted(), chan.name))
        for chan in ls:
            idstring = (' (id %s)' % (chan.id,) if self.debug_messages else '')
            memflag = ('*' if chan.member else ' ')
            privflag = (' (priv)' if chan.private else '')
            muteflag = (' (mute)' if chan.muted() else '')
            self.print(' %s%s%s%s%s' % (memflag, chan.name, idstring, privflag, muteflag))

    @uicommand('reload', isasync=True,
               arghelp='[team]',
               help='reload users and channels for a team')
    async def cmd_reload(self, args):
        """Command: reload user and channel data from a group.
        """
        team = self.parse_team_or_current(args)
        await team.load_connection_data()

    @uicommand('recap', isasync=True,
               arghelp='[channel] [interval]',
               help='recap an amount of time (default five minutes) on the current channel or a named channel')
    async def cmd_recap(self, args):
        """Command: recap messages from a channel, going back a given
        interval. The interval can be a number like "10" (minutes), or
        terms like "20m", "2h", "1d". The default is five minutes.
        """
        if args and args[0].startswith('#'):
            arg = args.pop(0)
            tup = self.parse_channelspec(arg[1:])
            (team, chanid) = tup
        else:
            if not self.curchannel:
                raise ArgException('No current team.')
            (teamid, chanid) = self.curchannel
            team = self.client.get_team(teamid)
            if not team:
                raise ArgException('Host not recognized: %s' % (teamid,))
        if not args:
            count = 5 * 60  # five minutes
        else:
            if not args[0]:
                raise ArgException('You must supply a number of minutes.')
            count = self.parse_interval(args[0])
            if count < 1:
                raise ArgException('Recap must be a (nonzero) amount of time.')

        await team.recap_channel(chanid, count)
        
    @uicommand('fetch', isasync=True,
               arghelp='[team] index|url',
               help='fetch a file from Slack given its name or index')
    async def cmd_fetch(self, args):
        """Command: Fetch a URL from the server
        """
        if not args:
            raise ArgException('You must supply an index or URL')
        target = args.pop()
        team = self.parse_team_or_current(args)

        match = pat_integer.match(target)
        if match:
            index = int(target)
            tup = self.files_by_index.get(index, None)
            if tup is None:
                raise ArgException('Host %s has no file index %d' % (team.short_name(), index,))
            url = tup[2]
            team = self.client.get_team(tup[1])
            if not team:
                raise ArgException('Host not recognized: %s' % (tup[1],))
        else:
            match = pat_url.match(target)
            if not match:
                raise ArgException('Not an index or URL: %s' % (target,))
            url = target

        tup = urllib.parse.urlparse(url)
        if not tup.netloc.lower().endswith('.slack.com'):
            self.print('URL does not appear to be a Slack URL: %s' % (url,))
            return
            
        self.print('Fetching %s...' % (url,))
        async with team.session.get(url, max_redirects=4) as resp:
            dat = await resp.read()
            if resp.status != 200:
                self.print('Got HTTP error %s' % (resp.status,))
                return
            filename = os.path.basename(tup.path)
            pathname = os.path.join(tempfile.gettempdir(), filename)
            fl = open(pathname, 'wb')
            fl.write(dat)
            fl.close()
            self.print('Fetched %d bytes: %s' % (len(dat), pathname,))
            opencmd = self.client.prefs.get('viewfile', None)
            if opencmd:
                args = opencmd.split(' ')
                args.append(pathname)
                proc = subprocess.Popen(args)
                while proc.poll() is None:
                    await asyncio.sleep(1)
        
    @uicommand('alias', 'aliases',
               arghelp='[team] alias,alias,...',
               help='set the aliases for a team')
    def cmd_alias(self, args):
        """Command: Set the aliases for a team.
        """
        if not args:
            # Show the current aliases
            if not self.curchannel:
                raise ArgException('No current team.')
            (teamid, chanid) = self.curchannel
            team = self.client.get_team(teamid)
            if not team:
                raise ArgException('Host not recognized: %s' % (teamid,))
            aliases = team.get_aliases()
            if not aliases:
                self.print('%s: no aliases set.' % (team.team_name,))
            else:
                self.print('%s: aliased to %s.' % (team.team_name, ','.join(aliases),))
            return
        val = args.pop()
        aliases = val.split(',')
        aliases = [ val.strip() for val in aliases ]
        aliases = [ val for val in aliases if val ]
        team = self.parse_team_or_current(args)
        self.client.prefs.team_put('aliases', aliases, team)
        self.print('%s: aliased to %s.' % (team.team_name, ','.join(aliases),))

    # This lists all the slash commands we recognize. The /help command
    # lists commands in this order.
    handler_list = [
        cmd_help,
        cmd_auth,
        cmd_connect,
        cmd_disconnect,
        cmd_teams,
        cmd_users,
        cmd_channels,
        cmd_reload,
        cmd_recap,
        cmd_fetch,
        cmd_alias,
        cmd_debug,
    ]

    # Construct the map of slash command strings to UICommand objects.
    # Both command names and command aliases are recognized as keys.
    handler_map = {}
    for han in handler_list:
        handler_map[han.name] = han
        if han.aliases:
            for alias in han.aliases:
                handler_map[alias] = han
    

from .teamdat import Host

