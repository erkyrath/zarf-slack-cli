# Zlack: a minimalist command-line Slack (and Mattermost) client

- Created by Andrew Plotkin <erkyrath@eblong.com>
- [MIT license][license]

[license]: ./LICENSE
[slackpost]: http://blog.zarfhome.com/2018/03/open-letter-slack-should-not.html

As you may know, Slack turned off their IRC and XMPP gateways on May 15th. (I [wrote an objection to this][slackpost], but it didn't help.) This project is a replacement for those gateways.

This is *not* a full-on Slack client! It's a replacement for the way I used the XMPP gateway: a little window in the corner of my screen where I can keep an eye on a few important Slack groups and type quick replies. Plain text only. It doesn't try to handle every fancy new feature that Slack rolls out. If you want to do something fancy, you pull up the official Slack client or the web page.

Zlack now also supports [Mattermost][]. Mattermost is an open-source team messaging service; it is similar to Slack in form and function.

[Slack]: https://slack.com/
[Mattermost]: https://mattermost.com/

## Installation

This tool is written in Python3. You'll need a recent version of that. You'll also need some packages:

> `pip3 install -r requirements.txt`

(Note that I am not using the official [Python Slack SDK][slack-sdk] library. Sorry! I'm using an extremely minimal subset of the Slack API; it was easier to write my own, at least back when I started this project.)

[slack-sdk]: https://pypi.org/project/slack-sdk/
[prompt-toolkit]: https://github.com/jonathanslenders/python-prompt-toolkit

## Setting up the Slack client

You'll have to create your own Slackapp client ID. This repository doesn't include any such ID, because these IDs should not be publicized.

### Creating a Slack app client ID

Visit [Slack's developer page][slackapp] and create a new app. Then, under "Permissions", add `http://localhost:8090/` as a redirect URL.

[slackapp]: https://api.slack.com/apps

Once you've done this, set the `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET` environment variables to the values shown on your "App Credentials" page. (Note that in earlier versions, these were named `ZLACK_...` with a Z. It's `SLACK_...` now.)

(The developer page suggests that you add "features or permissions scopes", but you don't have to. You're not going to be submitting this to Slack's App Directory.)

### Authenticating on Slack

Run `zlack.py`, and then type `/auth slack` to authenticate. (The environment variables must be set for this command to work.)

> `python3 zlack.py`

If you don't know what an environment variable is, you can type everything out on the command line:

> `python3 zlack.py --slack-client-id SLACK_CLIENT_ID --slack-client-secret SLACK_CLIENT_SECRET`

...where *SLACK_CLIENT_ID* and *SLACK_CLIENT_SECRET* are the long hex strings you got off the developer page.

When you type `/auth`, the script will display a Slack URL to visit. This will look like:

	https://slack.com/oauth/authorize?client_id=CLIENT_ID&scope=client&
	redirect_uri=http%3A//localhost%3A8090/&state=state_1234567

The script will then pause and wait for an authorization. It is listening on localhost port 8090.

Go to the Slack URL in your web browser. Slack will ask you to authorize the client. When you do, you will be redirected back to the localhost port. The script will then pick up the authorization and write your authentication token into `~/.zlack-tokens`.

You will only need to authenticate like this once (per machine). Your tokens will be saved; next time you run the script, it will connect right up. Unless you delete the `~/.zlack-tokens` file.

## Setting up the Mattermost client

This is basically the same procedure as for Slack.

### Creating a Mattermost app client ID

Make sure that your Mattermost server supports OAuth apps. In the System Console, go to "Integration Management" and turn on "Enable OAuth 2.0 Service Provider". 

Leave the System Console and go to "Integrations" in the main Mattermost menu. You should see an option for "OAuth 2.0 Applications". Select this and hit "Add OAuth 2.0 Application". Leave "Is Trusted" as "no". Add `http://localhost:8090/` as a callback URL.

(If you want to add the Zlack application as a normal user, rather than an admin, you will first need to go to "Permissions" and turn on "Manage OAuth Applications" for all users.)

Once you've done this, set the `MATTERMOST_CLIENT_ID` and `MATTERMOST_CLIENT_SECRET` environment variables to the values shown for the app.

### Authenticating on Mattermost

Run `zlack.py`, and then type `/auth mattermost` to authenticate. (The environment variables must be set for this command to work.)

When you type `/auth`, the script will display a Mattermost URL to visit. The script will then pause and wait for an authorization. It is listening on localhost port 8090.

Go to the URL in your web browser. Mattermost will ask you to authorize the client. When you do, you will be redirected back to the localhost port. The script will then pick up the authorization and write your authentication token into `~/.zlack-tokens`.

### Authenticating on Mattermost with a Personal Access Token

Mattermost also supports the idea of a Personal Access Token, which lets you authenticate directly. However, PATs have to be enabled for each user by the administrator, so this is less convenient.

If you have a PAT for your Mattermost account, type `/auth mattermost PAT` to authenticate with it. You don't need to follow a URL or click an authorize button.

## Running the client

Once you're authenticated, you can Slack away! Or Mattermost away! (They behave almost identically in this client.)

At first you have no channel set. So to send a message, type

> *\#team/channel* Message text to send.

(Again, you can use team aliases here.)

Once you send a message on a channel, it becomes the default channel. You don't need to use the channel prefix for subsequent messages. The current channel is always displayed as the input prompt, so you know where you're typing to.

The channel prefix can be any of:

- *\#team/channel* -- a channel of a particular team
- *\#channel* -- a channel of the current team
- *\#team/@user* -- the private message channel to a user on a particular team
- *\#@user* -- the private message channel to a user on the current team

(Multi-person message channels are not yet supported.)

If you type a semicolon on a line by itself, it will switch your default channel to match the last message you *received*. (If you're reading several conversations at once, look at the prompt to make sure you got what you thought!)

There are a handful of special commands, which start with a slash.

- */help* -- this list
- */auth slack* -- request authentication to a Slack team
- */auth mattermost* -- request authentication to a Mattermost team
- */connect [team]* -- connect (or reconnect) to a team
- */disconnect [team]* -- disconnect from a team
- */teams* -- list all teams you are authorized with
- */users [team]* -- list all users in the current team or a named team
- */channels [team]* -- list all channels in the current team or a named team
- */reload [team]* -- reload users and channels for a team
- */recap \[channel] \[interval]* -- recap an amount of time (default five minutes) on the current channel or a named channel
- */fetch [num]* -- download an attached file (by number)
- */alias [team] alias,alias,...* -- set the aliases for a team
- */debug [bool]* -- set stream debugging on/off or toggle


## Version history

- 3.0.0: Added Mattermost support!
- 2.0.0: Completely rewritten client library, fully async, no multithreading.
- 1.0.0: Original release. Used the [Python slackclient][slackclient] library. Had questionable thread-safety logic.

[slackclient]: https://github.com/slackapi/python-slackclient

## Work in progress

The client is usable, but -- of course -- not complete in any sense.

The client doesn't handle status changes such as new users or new channels. In particular, it won't recognize the new IM channel that is created the first time you talk to a given user. Use the */reload* command to fetch an up-to-date user and channel information.

Features the client does not currently handle which I might someday add:

- multi-person IM chat
- threaded messages
- the you-are-typing signal
- telling Slack what messages you've seen
- alerts and notifications
- muting some channels just in this client
- muting Slackbot entirely
- deactivating/muting an entire team

(Not listed: all the features which I don't intend to add. I'm not even trying to keep up with Slack here.)
