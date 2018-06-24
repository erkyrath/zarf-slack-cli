
class UI:
    def __init__(self, client):
        self.client = client

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

    def handle_message(self, obj):
        self.print('### %s' % (obj,))

    
