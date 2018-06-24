
class UI:
    def __init__(self, client):
        self.client = client

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
    
