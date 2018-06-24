
class UI:
    def __init__(self, client):
        self.client = client

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
        #if curchannel:
        #    (teamid, chanid) = curchannel
        #    prompt = '%s/%s> ' % (team_name(teamid), channel_name(teamid, chanid))
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
            self.lastchannel = (self.id, chanid)
            return
                
    def team_name(self, team):
        """Look up a team name, either as an alias (if available) or the
        full name. The argument can be a Team or team key string.
        """
        if not isinstance(team, Team):
            if team not in self.client.teams:
                return '???%s' % (team,)
            team = self.client.teams[team]
        ###
        #aliases = team.alias
        #if aliases:
        #    return aliases[0]
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

            
    

from .teamdat import Team
    
