#!/usr/bin/env python
import asyncio
from datetime import datetime
import discord
import json
import logging
import logging.config
import re
from signal import SIGINT, SIGTERM

from gametime import TimeCounter
from log import LOGGING_CONF
from music import MusicPlayer
from reminder import ReminderManager


log = logging.getLogger(__name__)
loop = asyncio.get_event_loop()


def get_time_string(seconds):
    hours = seconds / 3600
    minutes = (seconds / 60) % 60
    seconds = seconds % 60
    return '%0.2d:%02d:%02d' % (hours, minutes, seconds)


class Bot(object):

    """
    Make use of a conf.json file at work directory:
    {
        "email": "my.email@server.com",
        "password": "my_password",
        "admin_id": "id_of_the_bot_admin",
        "prefix": "!go",

        "music": {
            "avconv": false,

            # Optional, defaulted to 'opus'
            "opus": "opus shared library"
        }
    }
    """

    def __init__(self):

        with open('conf.json', 'r') as f:
            self.conf = json.loads(f.read())

        self.client = discord.Client(loop=loop)
        self.modules = dict()

        self.invite_regexp = re.compile(r'(?:https?\:\/\/)?discord\.gg\/(.+)')

        # Websocket handlers
        self.client.event(self.on_member_update)
        self.client.event(self.on_ready)
        self.client.event(self.on_message)

        self._start_time = datetime.now()
        self._commands = 0

    def __getattribute__(self, name):
        """
        getattr or take it from the modules dict
        """
        try:
            return super().__getattribute__(name)
        except AttributeError as exc:
            try:
                return self.modules[name]
            except KeyError:
                raise exc

    async def _add_module(self, cls, *args, **kwargs):
        module = cls(*args, **kwargs)
        try:
            await module.start()
        except Exception as exc:
            log.error('Module %s could not start properly', cls)
            log.error('dump: %s', exc)
        else:
            self.modules[cls.__name__.lower()] = module
            log.info('Module %s successfully started', cls)

    async def _stop_modules(self):
        """
        Stop all modules, with a timeout of 2 seconds
        """
        tasks = []
        for module in self.modules.values():
            tasks.append(asyncio.ensure_future(module.stop()))
        done, not_done = await asyncio.wait(tasks, timeout=2)
        if not_done:
            log.error('Stop tasks not done: %s', not_done)
        log.info('Modules stopped')

    async def start(self):
        asyncio.ensure_future(self._add_module(TimeCounter, loop=loop))
        asyncio.ensure_future(self._add_module(
            ReminderManager, self.client, loop=loop
        ))
        asyncio.ensure_future(self._add_module(
            MusicPlayer, self.client, **self.conf['music'], loop=loop
        ))
        await self.client.login(self.conf['email'], self.conf['password'])
        await self.client.connect()

    async def stop(self):
        await self._stop_modules()
        await self.client.close()

    def stop_signal(self):
        log.info('Closing')
        f = asyncio.ensure_future(self.stop())

        def end(res):
            log.info('Ending loop')
            loop.call_soon_threadsafe(loop.stop)

        f.add_done_callback(end)

    # Websocket handlers

    async def on_member_update(self, old, new):
        if new.id in self.timecounter.playing and not new.game:
            self.timecounter.done_counting(new.id)
        elif new.id not in self.timecounter.playing and new.game:
            self.timecounter.start_counting(new.id, new.game.name)

    async def on_ready(self):
        for server in self.client.servers:
            for member in server.members:
                if member.game:
                    self.timecounter.start_counting(member.id, member.game.name)
        log.info('everything ready')

    async def on_message(self, message):
        # If invite in private message, join server
        if message.channel.is_private:
            match = self.invite_regexp.match(message.content)
            if match and match.group(1):
                await self.client.accept_invite(match.group(1))
                log.info('Joined server, invite %s', match.group(1))
                await self.client.send_message(
                    message.author, 'Joined it, thanks :)')
                return

        if not message.content.startswith(self.conf['prefix']):
            return

        data = message.content.split(' ')
        if len(data) <= 1:
            log.debug('no command in message')
            return

        cmd = 'command_' + data[1]
        admin_cmd = 'admin_' + cmd
        handler = None

        # Check admin cmd
        if hasattr(self, admin_cmd):
            if message.author.id != self.conf['admin_id']:
                log.warning('Nope, not an admin')
            else:
                handler = getattr(self, admin_cmd)

        # Check regular cmd
        if not handler and hasattr(self, cmd):
            handler = getattr(self, cmd)

        if not handler:
            log.debug('no handler found')
            return

        # Go on.
        self._commands += 1
        await handler(message, *data[2:])

    # Commands

    async def command_play(self, message, *args):
        if not self.musicplayer:
            return

        if message.author.id not in self.musicplayer.whitelist:
            await self.client.send_message(message.channel, "Nah, not you.")
            return

        if len(args) < 2:
            return

        if self.musicplayer.player:
            self.musicplayer.stop()
            await self.musicplayer.play_future

        channel_name = ' '.join(args[0:-1])
        check = lambda c: c.name == channel_name and c.type == discord.ChannelType.voice
        channel = discord.utils.find(check, message.server.channels)
        if channel is None:
            await self.client.send_message(message.channel, 'Cannot find a voice channel by that name.')
            return

        self.musicplayer.play_song(channel, args[-1])

    async def command_stop(self, message, *args):
        if not self.musicplayer:
            return

        if message.author.id not in self.musicplayer.whitelist:
            await self.client.send_message(message.channel, "Nah, not you.")
            return

        self.musicplayer.stop()

    async def admin_command_add_user(self, message, *args):
        if not self.musicplayer:
            return

        if len(args) < 1:
            return

        self.musicplayer.add_user(args[0])
        await self.client.send_message(message.channel, "Done :)")

    async def admin_command_remove_user(self, message, *args):
        if not self.musicplayer:
            return

        if len(args) < 1:
            return

        self.musicplayer.remove_user(args[0])
        await self.client.send_message(message.channel, "Done :)")

    async def command_help(self, message, *args):
        await self.client.send_message(
            message.channel,
            "Available commands, all preceded by `!go`:\n"
            "`help     : show this help message`\n"
            "`stats    : show the bot's statistics`\n"
            "`source   : show the bot's source code (github)`\n"
            "`played   : show your game time`\n"
            "`reminder : remind you of something in <(w)d(x)h(y)m(z)s>`\n"
            "`play <channel name> <youtube url>`\n"
            "`stop     : Stop the music player`\n"
        )

    async def command_info(self, message, *args):
        await self.client.send_message(
            message.channel, "Your id: `%s`" % message.author.id)

    async def admin_command_add(self, message, *args):
        if len(args) <= 2:
            return

        # TODO: Handle cmd args with regexp
        user = args[0]
        game = ' '.join(args[1:-1])
        add_time = int(args[-1])

        old_time = self.timecounter.get(user).get(game, 0)
        self.timecounter.put(user, game, old_time + add_time)

        await self.client.send_message(message.channel, "done :)")

    async def command_played(self, message, *args):
        msg = ''
        played = self.timecounter.get(message.author.id)

        if played:
            msg += "As far as i'm aware, you played:\n"
            for game, time in played.items():
                msg += '`%s : %s`\n' % (game, get_time_string(time))
        else:
            msg = "I don't remember you playing anything :("

        await self.client.send_message(message.channel, msg)

    async def command_reminder(self, message, *args):
        if not args:
            return

        # New reminder
        time = args[0]
        msg = ' '.join(args[1:]) if len(args) >= 2 else 'ping!'

        if self.remindermanager.new(message.author.id, time, msg):
            response = 'Aight! I will ping you in %s' % time
        else:
            response = 'I could not understand that :('

        await self.client.send_message(message.channel, response)

    async def command_source(self, message, *args):
        await self.client.send_message(
            message.channel, 'https://github.com/gdraynz/discord-bot'
        )

    async def command_stats(self, message):
        users = 0
        for s in self.client.servers:
            users += len(s.members)

        msg = 'General statistics:\n'
        msg += '`Uptime            : %s`\n' % get_time_string((datetime.now() - self._start_time).total_seconds())
        msg += '`Users in touch    : %s in %s servers`\n' % (users, len(self.client.servers))
        msg += '`Commands answered : %d`\n' % self._commands
        msg += '`Users playing     : %d`\n' % len(self.timecounter.playing)
        await self.client.send_message(message.channel, msg)


def main():
    bot = Bot()

    loop.add_signal_handler(SIGINT, bot.stop_signal)
    loop.add_signal_handler(SIGTERM, bot.stop_signal)

    asyncio.ensure_future(bot.start())
    loop.run_forever()

    loop.close()


if __name__ == '__main__':
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('-l', '--logfile', action='store_true', help='Log file')

    args = parser.parse_args()

    if args.logfile:
        LOGGING_CONF['root']['handlers'] = ['logfile']

    logging.config.dictConfig(LOGGING_CONF)

    main()
