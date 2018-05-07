# Zlack: a minimalist Slack command-line client

- Created by Andrew Plotkin <erkyrath@eblong.com>
- [MIT license][license]

[license]: ./LICENSE
[slackpost]: http://blog.zarfhome.com/2018/03/open-letter-slack-should-not.html

As you probably know, Slack is turning off their IRC and XMPP gateways on May 15th. (I have [written an objection to this][slackpost], but it didn't help.)

I happen to like running a very lightweight Slack client in one corner of my screen, showing just a few of the most important Slack groups. The official Slack client is fine, but it's dwarf-star-weight and shows lots of groups, so I only run it some of the time. My lightweight solution used to be Adium and the XMPP gateway. Now I need a new solution.

## The very short form

Slack doesn't like people publicizing Slack app identifiers. So, you'll have to create your own. Visit [Slack's developer page][slackapp] and create one. Then, under "Permissions", add `http://localhost:8090/` as a redirect URL.

[slackapp]: https://api.slack.com/apps

Once you've done this, set the `ZLACK_CLIENT_ID` and `ZLACK_CLIENT_SECRET` environment variables to the values shown on your "App Credentials" page. 

Use `zlack-auth.py` to authenticate. (The environment variables must be set.) When you run it, it displays a Slack URL to visit. It also starts listening on localhost port 8090. Authorize the client at Slack, and then you will be redirected back to the localhost port. Once this succeeds, your authentication token will be written into `~/.zlack-tokens`.

Now you can run `zlack-client.py`. (This does not need the environment variables.) To send a message, type

> :team/channel Message text to send.

Once you send a message on a channel, you can use this shortcut:

> :channel Message sent to a channel of the most recently-used team.

If your input does not begin with a colon, the message will be sent to the most recently-used channel.

## Work in progress

I haven't written any documentation because I haven't settled on how it works yet. Use at your own risk. Actually, don't use it at all. I can't answer questions about it right now.

I'll keep hammering out the dents between now and May 15th. If it seems usable in real life, I'll write more.

