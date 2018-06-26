import re

pat_special_command = re.compile('/([a-z0-9?_-]+)', flags=re.IGNORECASE)
pat_dest_command = re.compile('#([^ ]+)')

pat_user_id = re.compile('@([a-z0-9._]+)', flags=re.IGNORECASE)
pat_encoded_user_id = re.compile('<@([a-z0-9_]+)>', flags=re.IGNORECASE)
pat_channel_id = re.compile('#([a-z0-9_-]+)', flags=re.IGNORECASE)
pat_encoded_channel_id = re.compile('<#([a-z0-9_]+)([|][a-z0-9_-]*)?>', flags=re.IGNORECASE)

pat_channel_command = re.compile('^(?:([a-z0-9_-]+)[/:])?([a-z0-9_-]+)$', flags=re.IGNORECASE)
pat_im_command = re.compile('^(?:([a-z0-9_-]+)[/:])?@([a-z0-9._]+)$', flags=re.IGNORECASE)
pat_defaultchan_command = re.compile('^([a-z0-9_-]+)[/:]$', flags=re.IGNORECASE)

class ArgException(Exception):
    pass

class UI:
    def __init__(self, client):
        self.client = client

        self.curchannel = None
        self.lastchannel = None
        self.debug_messages = False

    def print(self, msg):
        """Output a line of text. (Or several lines, as it could contain
        internal line breaks.) This is normally just print(), but you could
        subclass this and customize it.
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
        if self.curchannel:
            (teamkey, chanid) = self.curchannel
            return '%s/%s' % (self.team_name(teamkey), self.channel_name(teamkey, chanid))
        return ''

    def handle_message(self, msg, team):
        if self.debug_messages:
            self.print('Received (%s): %s' % (self.team_name(team), msg,))
            
        typ = msg.get('type')

        if typ is None and msg.get('reply_to'):
            # A reply to a message we sent.
            ###
            return
        
        if typ == 'hello':
            # Websocket-connected message.
            self.print('<Connected: %s>' % (self.team_name(team)))
            ### add five-second ping task? (to trigger socket timeout errors, if necessary)
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
                oldtext = self.decode_message(team.id, oldtext)
                val = '[%s/%s] (del) %s: %s' % (self.team_name(team), self.channel_name(team, chanid), self.user_name(team, userid), oldtext)
                self.print(val)
                return
            if subtype == 'message_changed':
                oldtext = ''
                if 'previous_message' in msg:
                    oldtext = msg.get('previous_message').get('text')
                    oldtext = self.decode_message(team.id, oldtext)
                userid = msg.get('message').get('user', '')
                newtext = msg.get('message').get('text')
                newtext = self.decode_message(team.id, newtext, msg.get('attachments'))
                if oldtext == newtext:
                    # Most likely this is a change to attachments, caused by Slack creating an image preview. Ignore.
                    return
                text = oldtext + '\n -> ' + newtext
                val = '[%s/%s] (edit) %s: %s' % (self.team_name(team), self.channel_name(team, chanid), self.user_name(team, userid), text)
                self.print(val)
                self.lastchannel = (team.id, chanid)
                return
            text = self.decode_message(team.id, msg.get('text'), msg.get('attachments'))
            subtypeflag = (' (%s)'%(subtype,) if subtype else '')
            colon = (':' if subtype != 'me_message' else '')
            val = '[%s/%s]%s %s%s %s' % (self.team_name(team), self.channel_name(team, chanid), subtypeflag, self.user_name(team, userid), colon, text)
            self.print(val)
            self.lastchannel = (team.id, chanid)
            return

    def handle_input(self, val):
        """Handle one input line from the player.
        """
        match = pat_special_command.match(val)
        if match:
            cmd = match.group(1).lower()
            args = val[ match.end() : ].split()
            tup = self.handler_map.get(cmd)
            if tup and isinstance(tup, str):
                # synonym
                tup = self.handler_map.get(tup)
            if not tup:
                self.print('Command not recognized: /%s' % (cmd,))
                return
            (handler, isasync) = tup
            if not isasync:
                try:
                    handler(self, args)
                except ArgException as ex:
                    self.print('Command /%s: %s' % (cmd, ex,))
                    return
                except Exception as ex:
                    self.print_exception(ex, '/'+cmd)
            else:
                task = self.client.evloop.create_task(handler(self, args))
                def callback(future):
                    ex = future.exception()
                    if ex and isinstance(ex, ArgException):
                        self.print('Command /%s: %s' % (cmd, ex,))
                    elif ex:
                        self.print_exception(ex, '/'+cmd)
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
            if not tup:
                return
            (team, chanid) = tup
            # Set the current channel.
            team.lastchannel = chanid
            self.curchannel = (team.key, chanid)
            self.lastchannel = self.curchannel
    
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
        text = self.encode_message(team, val)
        team.rtm_send({ 'type':'message', 'id':None, 'user':team.user_id, 'channel':chanid, 'text':text })
                
    def encode_message(self, team, val):
        """Encode a human-typed message into standard Slack form.
        """
        val = val.replace('&', '&amp;')
        val = val.replace('<', '&lt;')
        val = val.replace('>', '&gt;')
        # We try to locate @displayname references and convert them to
        # <@USERID>.
        val = pat_user_id.sub(lambda match:self.encode_exact_user_id(team, match), val)
        val = pat_channel_id.sub(lambda match:self.encode_exact_channel_id(team, match), val)
        return val
    
    def encode_exact_user_id(self, team, match):
        """Utility function used by encode_message. Given a match object from
        pat_user_id, return a <@USERID> substitution. If the match doesn't
        exactly match a user display name, we return the original string.    
        """
        orig = match.group(0)  # '@name'
        val = match.group(1)   # 'name'
        if val not in team.users_by_display_name:
            return orig
        return '<@' + team.users_by_display_name[val].id + '>'
    
    def encode_exact_channel_id(self, team, match):
        """Utility function used by encode_message. Given a match object from
        pat_channel_id, return a <#CHANID> substitution. If the match doesn't
        exactly match a channel name, we return the original string.    
        """
        orig = match.group(0)  # '#channel'
        val = match.group(1)   # 'channel'
        if val not in team.channels_by_name:
            return orig
        return '<#' + team.channels_by_name[val].id + '>'

    def decode_message(self, team, val, attachments=None):
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
        return val
    
    def team_name(self, team):
        """Look up a team name, either as an alias (if available) or the
        full name. The argument can be a Team or team key string.
        """
        if not isinstance(team, Team):
            if team not in self.client.teams:
                return '???%s' % (team,)
            team = self.client.teams[team]
        aliases = team.get_aliases()
        if aliases:
            return aliases[0]
        return team.team_name
    
    def channel_name(self, team, chanid):
        """Look up a channel name.
        """
        if not isinstance(team, Team):
            if team not in self.client.teams:
                return '???%s' % (chanid,)
            team = self.client.teams[team]
        if chanid not in team.channels:
            return '???%s' % (chanid,)
        return team.channels[chanid].name
    
    def user_name(self, team, userid):
        """Look up a user name (the displayname).
        """
        if not isinstance(team, Team):
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
    
        Returns (team, channelid). On error, prints a message and
        returns None.
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
                    self.print('No current team.')
                    return
                team = self.client.get_team(self.curchannel[0])
                if not team:
                    self.print('Team not recognized: %s' % (self.curchannel[0],))
                    return
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
                    self.print('No current team.')
                    return
                team = self.client.get_team(self.curchannel[0])
                if not team:
                    self.print('Team not recognized: %s' % (self.curchannel[0],))
                    return
            username = match.group(2)
            if username not in team.users_by_display_name:
                self.print('User not recognized: %s' % (username,))
                return
            chanid = team.users_by_display_name[username].im_channel
            if not chanid:
                self.print('No IM channel with user: %s' % (username,))
                return
        elif match_def:
            match = match_def
            # format: "TEAM/"
            team = self.parse_team(match.group(1))
            chanid = team.lastchannel
            if not chanid:
                self.print('No default channel for team: %s' % (self.team_name(team),))
                return
        else:
            self.print('Channel spec not recognized: %s' % (val,))
            return
    
        return (team, chanid)

    def parse_team(self, val):
        """Parse a team name, ID, or alias. Returns the Team entry.
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
        raise ArgException('Team not recognized: %s' % (val,))
    
    def parse_channel(self, team, val):
        """Parse a channel name (a bare channel, no # or team prefix)
        for a given Team. Returns the channel ID.
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

    def parse_bool(self, val):
        val = val.lower()
        if val.startswith('1') or val.startswith('y') or val.startswith('t') or val=='on':
            return True
        if val.startswith('0') or val.startswith('n') or val.startswith('f') or val=='off':
            return False
        raise ArgException('Boolean argument expected')

    def parse_team_or_current(self, args):
        if not args:
            if not self.curchannel:
                raise ArgException('No current team.')
            team = self.client.get_team(self.curchannel[0])
            if not team:
                raise ArgException('Team not recognized: %s' % (self.team_name(self.curchannel[0]),))
        elif len(args) == 1:
            team = self.parse_team(args[0])
        else:
            raise ArgException('Expected zero or one arguments')
        return team
    
    def cmd_help(self, args):
        """Command: display the command list.
        """
        self.print('/help -- this list')
        self.print('/auth -- request authentication to a Slack team')
        self.print('/teams -- list all teams you are authorized with')
        self.print('/connect [team] -- connect (or reconnect) to a team')
        self.print('/disconnect [team] -- disconnect from a team')
        self.print('/reload [team] -- reload users and channels for a team')
        self.print('/channels [team] -- list all channels in the current team or a named team')
        self.print('/users [team] -- list all users in the current team or a named team')
        self.print('/recap [channel] [minutes] -- recap an amount of time (default five minutes) on the current channel or a named channel')
        self.print('/debug [bool] -- set stream debugging on/off or toggle')

    def cmd_auth(self, args):
        """Command: authenticate to a Slack team.
        """
        self.client.begin_auth()
        
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

    def cmd_connect(self, args):
        """Command: connect to a group. If we're already connected, disconnect
        and then reconnect.
        """
        team = self.parse_team_or_current(args)
        team.rtm_connect()

    def cmd_disconnect(self, args):
        """Command: disconnect from a group. This only applies to the RTM
        connection.
        """
        team = self.parse_team_or_current(args)
        if not team.rtm_connected():
            self.print('Team not connected: %s' % (self.team_name(team),))
            return
        team.rtm_disconnect()
        if self.curchannel and self.curchannel[0] == team.key:
            self.curchannel = None

    def cmd_teams(self, args):
        """Command: display the list of teams. Asterisk indicates an active
        RTM connection.
        """
        if args:
            raise ArgException('Expected no arguments')
        ls = list(self.client.teams.values())
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
    
    def cmd_users(self, args):
        """Command: display the list of users.
        """
        team = self.parse_team_or_current(args)
        ls = list(team.users.values())
        ls.sort(key = lambda user:user.name)
        for user in ls:
            idstring = (' (id %s)' % (user.id,) if self.debug_messages else '')
            self.print('  %s%s: %s' % (user.name, idstring, user.real_name))
    
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

    async def cmd_reload(self, args):
        """Command: reload user and channel data from a group.
        """
        team = self.parse_team_or_current(args)
        await team.load_connection_data()

    handler_map = {
        'help': (cmd_help, False),
        '?': 'help',
        'auth': (cmd_auth, False),
        'debug': (cmd_debug, False),
        'connect': (cmd_connect, False),
        'disconnect': (cmd_disconnect, False),
        'teams': (cmd_teams, False),
        'users': (cmd_users, False),
        'channels': (cmd_channels, False),
        'reload': (cmd_reload, True),
    }
    

from .teamdat import Team
    
