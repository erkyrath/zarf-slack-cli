from collections import OrderedDict
import aiohttp
import aiohttp.web

class Protocol:
    key = None
    hostclass = None

    def __init__(self, client):
        self.client = client
        self.teams = OrderedDict()   # team.key to Host

    def __repr__(self):
        return '<%s (%s)>' % (self.__class__.__name__, self.key,)

    def create_team(self, map):
        # Call the HostClass's constructor.
        cla = self.hostclass
        team = cla(self, map)
        
        # Add it to both our team list and the master team list.
        self.teams[team.key] = team
        self.client.teams[team.key] = team
        
        return team

    def print(self, msg):
        """Output a line of text. (Or several lines, as it could contain
        internal line breaks.) You typically won't want to customize this;
        instead, replace the Client.print() method.
        """
        self.client.print(msg)

    def print_exception(self, ex, label='zlack'):
        """Convenience function to print an exception using self.print().
        If ex is None, this does nothing (so you can conveniently use it
        when you only *might* have an exception). If --debugexceptions is
        set, this prints complete stack traces.
        """
        self.client.print_exception(ex, '%s (%s)' % (label, self.key))

    def construct_auth_handler(self, future, statecheck):
        """Construct a handler for aiohttp.web.Server.
        This handler accepts the web request on port 8090. When a valid
        request is received, it sets a value in the passed-in future.
        (This is generic to all OAuth implementation, so it lives in
        Protocol.)
        """
        
        async def handler(request):
            map = request.query
            message = '???'
    
            if 'code' not in map:
                message = 'No code found.'
            elif 'state' not in map:
                message = 'No state field found.'
            elif map['state'] != statecheck:
                message = 'State field did not match.'
            else:
                code = map['code']
                future.set_result(code)
                message = 'Auth code received: %s\n' % (code,)
            
            return aiohttp.web.Response(text=message)
        
        return handler


class Host:
    protocol = None
    protocolkey = None
    
    # team.id: identifier, unique within protocol
    # team.key: "protocol:id"

    def print(self, msg):
        """Output a line of text. (Or several lines, as it could contain
        internal line breaks.) You typically won't want to customize this;
        instead, replace the Client.print() method.
        """
        self.client.print(msg)

    def print_exception(self, ex, label='zlack'):
        """Convenience function to print an exception using self.print().
        If ex is None, this does nothing (so you can conveniently use it
        when you only *might* have an exception). If --debugexceptions is
        set, this prints complete stack traces.
        """
        self.client.print_exception(ex, '%s (%s)' % (label, self.short_name()))

    
class Channel:
    pass

class User:
    pass
