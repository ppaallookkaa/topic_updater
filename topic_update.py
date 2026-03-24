"""Sopel plugin: .topic_add — append chat log quotes to channel topic."""
from collections import deque
from datetime import datetime, timezone
from sopel import plugin, formatting
from sopel.privileges import AccessLevel
import json
import os
import sqlite3
import threading

# ── Constants ────────────────────────────────────────────────────────────────
IGNORED_NICKS = {'igor', 'ptpinfo'}
BUFFER_SIZE = 200
PM_TIMEOUT = 300   # seconds
POLL_TIMEOUT = 120  # seconds
HISTORY_MAX = 100  # max entries per channel kept in history file
# Same relative path quote.py uses — resolved at runtime so both plugins
# find the same file regardless of working directory
QUOTES_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'quotes.db')

# ── State ─────────────────────────────────────────────────────────────────────
_buffers: dict = {}    # channel -> deque of (mode_prefix, nick, message)
_polls: dict = {}      # channel -> poll state dict
_pm_convos: dict = {}  # nick -> PM conversation state dict
_history_lock = threading.Lock()

# ── Pure Helpers ──────────────────────────────────────────────────────────────

def get_mode_prefix(privs: int) -> str:
    """Return the highest IRC mode prefix character for a privilege bitmask."""
    if privs & AccessLevel.OWNER:  return '~'
    if privs & AccessLevel.ADMIN:  return '&'
    if privs & AccessLevel.OP:     return '@'
    if privs & AccessLevel.HALFOP: return '%'
    if privs & AccessLevel.VOICE:  return '+'
    return ''


def parse_topic_add_args(args: str) -> dict:
    """
    Parse .topic_add argument string into a typed dict.

    Returns one of:
      {'type': 'nick_index', 'nick': str, 'index': int}
      {'type': 'nick',       'nick': str}
      {'type': 'index',      'index': int}
      {'type': 'empty'}
    """
    parts = args.strip().split()
    if not parts:
        return {'type': 'empty'}
    if len(parts) == 1:
        if parts[0].isdigit():
            return {'type': 'index', 'index': int(parts[0])}
        return {'type': 'nick', 'nick': parts[0]}
    # two or more parts: first is nick, second is index (ignore extras)
    if parts[1].isdigit():
        return {'type': 'nick_index', 'nick': parts[0], 'index': int(parts[1])}
    # second part isn't a number — treat as nick only
    return {'type': 'nick', 'nick': parts[0]}


def format_quote(mode_prefix: str, nick: str, message: str) -> str:
    """Format a quote as <{mode}{nick}> {message}."""
    return f'<{mode_prefix}{nick}> {message}'


def build_new_topic(current_topic: str, new_segment: str) -> str:
    """Append new_segment to current_topic, separated by ' | '."""
    if not current_topic:
        return new_segment
    return f'{current_topic} | {new_segment}'


def _get_topiclen(bot) -> int:
    """Read TOPICLEN from server ISUPPORT, defaulting to 307."""
    val = getattr(bot.isupport, 'TOPICLEN', None)
    try:
        return int(val) if val is not None else 307
    except (TypeError, ValueError):
        return 307


def _truncate_to_topiclen(topic: str, topiclen: int) -> str:
    """Truncate topic to at most topiclen UTF-8 bytes."""
    encoded = topic.encode('utf-8')
    if len(encoded) <= topiclen:
        return topic
    return encoded[:topiclen].decode('utf-8', errors='ignore')


def check_topic_fit(
    current_topic: str,
    new_segment: str,
    topiclen: int,
) -> tuple[bool, int, list[str]]:
    """
    Check if appending new_segment to current_topic fits within topiclen bytes.

    Returns (fits, overage_bytes, segments).
    segments is current_topic split on ' | '.
    overage_bytes is 0 when fits.
    """
    candidate = build_new_topic(current_topic, new_segment)
    byte_len = len(candidate.encode('utf-8'))
    segments = current_topic.split(' | ')
    if byte_len <= topiclen:
        return True, 0, segments
    return False, byte_len - topiclen, segments


# ── Buffer Operations ─────────────────────────────────────────────────────────

def store_message_in_buffer(channel: str, nick: str, message: str, mode_prefix: str):
    """Append (mode_prefix, nick, message) to the channel's message buffer."""
    if channel not in _buffers:
        _buffers[channel] = deque(maxlen=BUFFER_SIZE)
    _buffers[channel].append((mode_prefix, nick, message))


def get_message_from_nick(
    channel: str, nick: str, index: int
) -> tuple | None:
    """
    Return the index-th most recent message from nick in channel (1-based).
    Returns (mode_prefix, nick, message) or None.
    Nick matching is case-insensitive.
    """
    messages = list(_buffers.get(channel, []))
    nick_messages = [
        entry for entry in reversed(messages)
        if entry[1].lower() == nick.lower()
    ]
    if index < 1 or index > len(nick_messages):
        return None
    return nick_messages[index - 1]


def get_message_overall(channel: str, index: int) -> tuple | None:
    """
    Return the index-th most recent message overall in channel (1-based).
    Returns (mode_prefix, nick, message) or None.
    """
    messages = list(_buffers.get(channel, []))
    if index < 1 or index > len(messages):
        return None
    return messages[-index]


# ── Listeners ─────────────────────────────────────────────────────────────────

@plugin.rule('.*')
@plugin.priority('low')
def store_message_listener(bot, trigger):
    """Record channel messages into the per-channel buffer."""
    if trigger.is_privmsg:
        return
    if str(trigger.nick).lower() in IGNORED_NICKS:
        return
    message = trigger.group(0)
    if message.startswith(bot.config.core.help_prefix):
        return
    channel = str(trigger.sender)
    nick = str(trigger.nick)
    privs = bot.channels.get(channel, {})
    if hasattr(privs, 'privileges'):
        priv_val = privs.privileges.get(nick, 0) or 0
    else:
        priv_val = 0
    mode_prefix = get_mode_prefix(int(priv_val))
    store_message_in_buffer(channel, nick, message, mode_prefix)


# ── Topic Application ─────────────────────────────────────────────────────────

def _history_path(bot) -> str:
    return os.path.join(bot.settings.core.homedir, 'topic_history.jsonl')


def _append_history(bot, channel: str, topic: str) -> None:
    """Append topic to the flat history file (thread-safe)."""
    if not topic:
        return
    entry = json.dumps({
        'ch': channel,
        'ts': datetime.now(timezone.utc).isoformat(),
        'topic': topic,
    })
    with _history_lock:
        with open(_history_path(bot), 'a', encoding='utf-8') as fh:
            fh.write(entry + '\n')


def _load_history(bot, channel: str, n: int = 10) -> list:
    """Return the last n topics for channel, most recent first."""
    try:
        with _history_lock:
            with open(_history_path(bot), 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
    except FileNotFoundError:
        return []
    results = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get('ch') == channel:
                results.append(entry['topic'])
                if len(results) >= n:
                    break
        except (json.JSONDecodeError, KeyError):
            continue
    return results


def _save_to_quotes_db(nick: str, channel: str, message: str) -> None:
    """Save a quote to the shared quotes.db (same DB as quote.py)."""
    try:
        conn = sqlite3.connect(QUOTES_DB)
        conn.execute(
            'INSERT INTO quotes (nick, hostname, channel, message, timestamp) '
            'VALUES (?, ?, ?, ?, ?)',
            (nick, '', channel, message, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # don't let a DB error break the topic update


def _bot_can_set_topic(bot, channel: str) -> bool:
    """Return True if the bot has at least OP privileges in channel."""
    privs = bot.channels.get(channel)
    if not privs or not hasattr(privs, 'privileges'):
        return False
    priv_val = int(privs.privileges.get(str(bot.nick), 0) or 0)
    return bool(priv_val & (AccessLevel.OP | AccessLevel.ADMIN | AccessLevel.OWNER))


def apply_topic(bot, channel: str, new_topic: str, requester: str = '') -> bool:
    """
    Send TOPIC command to IRC server.
    Returns True on success, False if bot lacks channel op.
    PMs requester if provided and bot lacks permission.
    """
    if not _bot_can_set_topic(bot, channel):
        if requester:
            bot.say(
                f"I don't have permission to set the topic in {channel}.",
                requester,
            )
        return False
    chan = bot.channels.get(channel)
    prev = (chan.topic or '') if chan else ''
    _append_history(bot, channel, prev)
    bot.write(('TOPIC', f'{channel} :{new_topic}'))
    return True


def resolve_quote_for_channel(channel: str, parsed: dict) -> tuple | None:
    """
    Resolve parsed .topic_add args to (mode_prefix, nick, message) or None.
    """
    ptype = parsed['type']
    if ptype == 'empty':
        return None
    if ptype == 'nick_index':
        return get_message_from_nick(channel, parsed['nick'], parsed['index'])
    if ptype == 'nick':
        return get_message_from_nick(channel, parsed['nick'], 1)
    if ptype == 'index':
        return get_message_overall(channel, parsed['index'])
    return None


# ── PM Helpers ────────────────────────────────────────────────────────────────

def _cancel_pm_convo(nick: str):
    """Cancel any existing PM conversation for nick."""
    existing = _pm_convos.pop(nick, None)
    if existing and existing.get('timer'):
        existing['timer'].cancel()


def _make_pm_timer(bot, nick: str) -> threading.Timer:
    """Create a 5-minute inactivity timer that clears the PM convo."""
    def _timeout():
        _pm_convos.pop(nick, None)
    t = threading.Timer(PM_TIMEOUT, _timeout)
    t.daemon = True
    t.start()
    return t


def find_fit_combos(
    segments: list,
    new_quote: str,
    topiclen: int,
    max_combos: int = 6,
) -> list:
    """
    Find combinations of (replace_idx, remove_idxs) where replacing the
    segment at replace_idx with new_quote and dropping segments at remove_idxs
    produces a topic that fits within topiclen bytes.

    For each replacement target, the minimum additional removals are found
    greedily (longest segments removed first). Returns up to max_combos results
    sorted by fewest removals then by replace_idx.
    """
    results = []
    n = len(segments)

    for replace_idx in range(n):
        working = [new_quote if i == replace_idx else seg for i, seg in enumerate(segments)]
        topic = ' | '.join(working)
        if len(topic.encode('utf-8')) <= topiclen:
            results.append((replace_idx, ()))
            continue

        # Greedily remove longest other segments until it fits
        other_idxs = sorted(
            (i for i in range(n) if i != replace_idx),
            key=lambda i: len(segments[i].encode('utf-8')),
            reverse=True,
        )
        remove_idxs: list = []
        for idx in other_idxs:
            remove_idxs.append(idx)
            remaining = [seg for i, seg in enumerate(working) if i not in remove_idxs]
            if len(' | '.join(remaining).encode('utf-8')) <= topiclen:
                results.append((replace_idx, tuple(sorted(remove_idxs))))
                break

    results.sort(key=lambda x: (len(x[1]), x[0]))
    return results[:max_combos]


def start_overflow_flow(
    bot,
    requester: str,
    channel: str,
    new_quote: str,
    segments: list,
    overage: int,
    quote_nick: str = '',
    quote_message: str = '',
):
    """PM the requester with combo options and await their choice."""
    _cancel_pm_convo(requester)

    topiclen = _get_topiclen(bot)

    if len(new_quote.encode('utf-8')) > topiclen:
        bot.say(
            f'Your quote is too long to fit as a topic segment '
            f'({len(new_quote.encode("utf-8"))}/{topiclen} bytes).',
            requester,
        )
        return

    combos = find_fit_combos(segments, new_quote, topiclen)
    if not combos:
        bot.say('No valid replacement found — topic cannot accommodate this quote.', requester)
        return

    lines = ['Topic is full. Choose what to replace:', '']
    for i, (replace_idx, remove_idxs) in enumerate(combos, 1):
        all_gone = [segments[replace_idx]] + [segments[j] for j in remove_idxs]
        if len(all_gone) == 1:
            label = f'"{all_gone[0]}"'
        elif len(all_gone) == 2:
            label = f'"{all_gone[0]}" and "{all_gone[1]}"'
        else:
            label = ', '.join(f'"{s}"' for s in all_gone[:-1]) + f', and "{all_gone[-1]}"'
        new_segs = [
            new_quote if j == replace_idx else seg
            for j, seg in enumerate(segments)
            if j not in remove_idxs or j == replace_idx
        ]
        result_len = len(' | '.join(new_segs).encode('utf-8'))
        lines.append(f'  {i}. Replaces {label} ({result_len}/{topiclen})')
    lines += ['', 'Reply with a number, or "cancel".']

    for line in lines:
        bot.say(line, requester)

    _pm_convos[requester] = {
        'state': 'awaiting_segment',
        'channel': channel,
        'staged_quote': new_quote,
        'segments': segments,
        'combos': combos,
        'quote_nick': quote_nick,
        'quote_message': quote_message,
        'timer': _make_pm_timer(bot, requester),
    }


def handle_segment_selection(bot, nick: str, reply: str):
    """Process a PM reply during awaiting_segment state."""
    state = _pm_convos.get(nick)
    if not state or state.get('state') != 'awaiting_segment':
        return

    reply = reply.strip()
    if reply.lower() == 'cancel':
        state['timer'].cancel()
        _pm_convos.pop(nick, None)
        bot.say('Topic update cancelled.', nick)
        return

    combos = state['combos']
    if reply.isdigit():
        choice = int(reply)
        if 1 <= choice <= len(combos):
            replace_idx, remove_idxs = combos[choice - 1]
            old_segment = state['segments'][replace_idx]
            state['timer'].cancel()
            _pm_convos.pop(nick, None)
            start_poll(
                bot, state['channel'], old_segment,
                state['staged_quote'], replace_idx, nick,
                remove_idxs=remove_idxs,
                all_segments=state['segments'],
                quote_nick=state.get('quote_nick', ''),
                quote_message=state.get('quote_message', ''),
            )
            return

    # Invalid reply — re-prompt and restart inactivity timer
    state['timer'].cancel()
    state['timer'] = _make_pm_timer(bot, nick)
    bot.say(f'Please reply with a number between 1 and {len(combos)}, or "cancel".', nick)


# ── Poll Management ───────────────────────────────────────────────────────────

def start_poll(
    bot,
    channel: str,
    old_segment: str,
    new_quote: str,
    segment_index: int,
    requester: str,
    remove_idxs: tuple = (),
    all_segments: list = None,
    restore_topic: str = None,
    quote_nick: str = '',
    quote_message: str = '',
):
    """Post a formatted poll in channel; reject if a poll is already running."""
    if channel in _polls:
        bot.say(
            f'A poll is already running in {channel}, try again after it concludes.',
            requester,
        )
        return

    current_topic = bot.channels[channel].topic or ''
    topiclen = _get_topiclen(bot)

    def _resolve():
        resolve_poll(bot, channel)

    timer = threading.Timer(POLL_TIMEOUT, _resolve)
    timer.daemon = True

    _polls[channel] = {
        'old_segment': old_segment,
        'new_quote': new_quote,
        'segment_index': segment_index,
        'requester': requester,
        'yes_votes': set(),
        'no_votes': set(),
        'timer': timer,
        'topic_snapshot': current_topic,
        'topiclen': topiclen,
        'remove_idxs': remove_idxs,
        'all_segments': all_segments,
        'restore_topic': restore_topic,
        'quote_nick': quote_nick,
        'quote_message': quote_message,
    }

    minus   = formatting.color('--', formatting.colors.RED)
    plus    = formatting.color('++', formatting.colors.GREEN)
    yes_fmt = formatting.color('yes', formatting.colors.GREEN)
    no_fmt  = formatting.color('no', formatting.colors.RED)
    header  = formatting.bold('TOPIC VOTE')

    bot.say(f'🗳️  {header}', channel)
    if restore_topic is not None:
        restore_fmt = formatting.color(f'"{restore_topic}"', formatting.colors.CYAN)
        bot.say(f'Restore: {plus} {restore_fmt}', channel)
    else:
        old_fmt = formatting.color(f'"{old_segment}"', formatting.colors.ORANGE)
        new_fmt = formatting.color(f'"{new_quote}"', formatting.colors.CYAN)
        bot.say(f'Replace: {minus} {old_fmt}', channel)
        bot.say(f'   With: {plus} {new_fmt}', channel)
        if remove_idxs and all_segments:
            dropped = [all_segments[j] for j in remove_idxs]
            if len(dropped) == 1:
                drops_fmt = f'"{dropped[0]}"'
            else:
                drops_fmt = ', '.join(f'"{s}"' for s in dropped[:-1]) + f', and "{dropped[-1]}"'
            bot.say(f'  Drops: {drops_fmt}', channel)
    bot.say(
        f'Vote {yes_fmt} / {no_fmt} within 2 minutes. Majority wins.',
        channel,
    )

    # Start timer after announcing — 120s begins once the poll is visible
    timer.start()


@plugin.rule(r'^(yes|y|no|n)$')
@plugin.priority('low')
def handle_vote(bot, trigger):
    """Record a yes/no vote for an active poll in the channel."""
    channel = str(trigger.sender)
    poll = _polls.get(channel)
    if not poll:
        return
    nick = str(trigger.nick)
    if nick.lower() in IGNORED_NICKS:
        return
    if nick in poll['yes_votes'] or nick in poll['no_votes']:
        return  # blocks both duplicate votes and vote changes by design
    vote = trigger.group(0).lower()
    if vote in ('yes', 'y'):
        poll['yes_votes'].add(nick)
    else:
        poll['no_votes'].add(nick)


def resolve_poll(bot, channel: str):
    """
    Timer callback: tally votes and apply or reject the topic replacement.
    Uses topic_snapshot captured at poll creation — does NOT re-read bot.channels.
    bot.say() and bot.write() are thread-safe in Sopel 8.x.
    """
    poll = _polls.pop(channel, None)
    if not poll:
        return
    poll['timer'].cancel()  # safe to call even if timer already fired

    yes_count = len(poll['yes_votes'])
    no_count  = len(poll['no_votes'])

    if yes_count == 0 and no_count == 0:
        bot.say('No votes cast, topic unchanged.', channel)
        return

    if yes_count > no_count:
        topiclen = poll.get('topiclen', 307)
        restore_topic = poll.get('restore_topic')

        if restore_topic is not None:
            # Full topic restore (.topic_back / .topic_revert)
            new_topic = _truncate_to_topiclen(restore_topic, topiclen)
        else:
            # Segment replacement (.topic_add overflow)
            remove_idxs = set(poll.get('remove_idxs', ()))
            all_segments = poll.get('all_segments')
            if all_segments is not None:
                idx = poll['segment_index']
                new_segs = [
                    poll['new_quote'] if i == idx else seg
                    for i, seg in enumerate(all_segments)
                    if i not in remove_idxs or i == idx
                ]
            else:
                segs = poll['topic_snapshot'].split(' | ')
                idx = poll['segment_index']
                if 0 <= idx < len(segs):
                    segs[idx] = poll['new_quote']
                else:
                    segs.append(poll['new_quote'])
                new_segs = segs
            new_topic = _truncate_to_topiclen(' | '.join(new_segs), topiclen)
            if poll.get('quote_nick') and poll.get('quote_message'):
                _save_to_quotes_db(poll['quote_nick'], channel, poll['quote_message'])

        if not apply_topic(bot, channel, new_topic, requester=poll['requester']):
            return
        result = formatting.color(
            formatting.bold('✅ Topic updated.'), formatting.colors.GREEN
        )
    else:
        result = formatting.color(
            formatting.bold('❌ Vote failed, topic unchanged.'), formatting.colors.RED
        )

    bot.say(result, channel)


def start_noargs_flow(bot, trigger):
    """Begin interactive PM flow for .topic_add with no arguments."""
    nick = str(trigger.nick)
    channel = str(trigger.sender)
    _cancel_pm_convo(nick)

    if channel not in bot.channels:
        bot.say("I don't appear to be in that channel.", nick)
        return
    current_topic = bot.channels[channel].topic or ''
    topiclen = _get_topiclen(bot)
    # Calculate bytes available: topiclen minus current topic minus ' | ' separator
    separator = ' | ' if current_topic else ''
    used = len((current_topic + separator).encode('utf-8'))
    available = topiclen - used

    bot.say(f'What do you want to add to the topic? ({available} bytes available)', nick)
    _pm_convos[nick] = {
        'state': 'awaiting_text',
        'channel': channel,
        'staged_quote': '',
        'segments': [],
        'timer': _make_pm_timer(bot, nick),
    }


def handle_text_entry(bot, nick: str, channel: str, text: str):
    """Process free-text PM reply for awaiting_text state."""
    state = _pm_convos.get(nick)
    if not state:
        return
    state['timer'].cancel()

    current_topic = bot.channels[channel].topic or ''
    topiclen = _get_topiclen(bot)
    new_quote = text.strip()
    fits, overage, segments = check_topic_fit(current_topic, new_quote, topiclen)

    _pm_convos.pop(nick, None)

    if fits:
        new_topic = build_new_topic(current_topic, new_quote)
        if apply_topic(bot, channel, new_topic, requester=nick):
            _save_to_quotes_db(nick, channel, new_quote)
            bot.say(f'Topic updated by {nick}.', channel)
    else:
        start_overflow_flow(bot, nick, channel, new_quote, segments, overage,
                            quote_nick=nick, quote_message=new_quote)


# ── Command Handler ───────────────────────────────────────────────────────────

@plugin.command('topic_add')
@plugin.require_chanmsg('Please use .topic_add in a channel.')
def topic_add(bot, trigger):
    """Append a chat log quote to the channel topic.

    Usage:
      .topic_add nick #  — #th most recent message from nick
      .topic_add nick    — most recent message from nick
      .topic_add #       — #th most recent message overall
      .topic_add         — PM flow to enter text manually
    """
    channel = str(trigger.sender)
    requester = str(trigger.nick)
    args = trigger.group(2) or ''
    parsed = parse_topic_add_args(args)

    if parsed['type'] == 'empty':
        start_noargs_flow(bot, trigger)
        return

    entry = resolve_quote_for_channel(channel, parsed)
    if entry is None:
        if parsed['type'] in ('nick', 'nick_index'):
            nick = parsed['nick']
            count_in_buffer = sum(
                1 for e in _buffers.get(channel, [])
                if e[1].lower() == nick.lower()
            )
            if count_in_buffer == 0:
                bot.say(
                    f'No messages found from {nick} in the last {BUFFER_SIZE} lines.',
                    requester,
                )
            else:
                bot.say(
                    f'Only {count_in_buffer} messages found from {nick}, '
                    f"can't retrieve #{parsed.get('index', 1)}.",
                    requester,
                )
        else:
            total = len(_buffers.get(channel, []))
            bot.say(
                f'Only {total} messages in buffer, '
                f"can't retrieve #{parsed['index']}.",
                requester,
            )
        return

    mode_prefix, nick, message = entry
    new_quote = format_quote(mode_prefix, nick, message)
    current_topic = bot.channels[channel].topic or ''
    topiclen = _get_topiclen(bot)

    fits, overage, segments = check_topic_fit(current_topic, new_quote, topiclen)

    if fits:
        new_topic = build_new_topic(current_topic, new_quote)
        if apply_topic(bot, channel, new_topic, requester=requester):
            _save_to_quotes_db(nick, channel, message)
            bot.say(f'Topic updated by {requester}.', channel)
    else:
        start_overflow_flow(bot, requester, channel, new_quote, segments, overage,
                            quote_nick=nick, quote_message=message)


def handle_revert_selection(bot, nick: str, reply: str):
    """Process a PM reply during awaiting_revert state."""
    state = _pm_convos.get(nick)
    if not state or state.get('state') != 'awaiting_revert':
        return
    reply = reply.strip()
    if reply.lower() == 'cancel':
        state['timer'].cancel()
        _pm_convos.pop(nick, None)
        bot.say('Cancelled.', nick)
        return
    history = state['history']
    if reply.isdigit():
        idx = int(reply)
        if 1 <= idx <= len(history):
            topic = history[idx - 1]
            state['timer'].cancel()
            _pm_convos.pop(nick, None)
            start_poll(bot, state['channel'], '', '', 0, nick, restore_topic=topic)
            return
    state['timer'].cancel()
    state['timer'] = _make_pm_timer(bot, nick)
    bot.say(f'Please reply with a number between 1 and {len(history)}, or "cancel".', nick)


@plugin.command('topic_back')
@plugin.require_chanmsg
def topic_back(bot, trigger):
    """Start a vote to restore the most recent previous topic."""
    channel = trigger.sender
    requester = trigger.nick
    if channel in _polls:
        bot.say(
            f'A poll is already running in {channel}, try again after it concludes.',
            requester,
        )
        return
    history = _load_history(bot, channel, n=1)
    if not history:
        bot.say('No topic history for this channel.', channel)
        return
    start_poll(bot, channel, '', '', 0, requester, restore_topic=history[0])


@plugin.command('topic_revert')
@plugin.require_chanmsg
def topic_revert(bot, trigger):
    """PM the requester with recent topic history to pick one to restore."""
    channel = trigger.sender
    requester = trigger.nick
    if channel in _polls:
        bot.say(
            f'A poll is already running in {channel}, try again after it concludes.',
            requester,
        )
        return
    history = _load_history(bot, channel, n=10)
    if not history:
        bot.say('No topic history for this channel.', channel)
        return
    lines = [f'Recent topics for {channel}:', '']
    for i, topic in enumerate(history, 1):
        lines.append(f'  {i}. {topic}')
    lines += ['', 'Reply with a number to start a restore vote, or "cancel".']
    for line in lines:
        bot.say(line, requester)
    _cancel_pm_convo(requester)
    _pm_convos[requester] = {
        'state': 'awaiting_revert',
        'channel': channel,
        'history': history,
        'timer': _make_pm_timer(bot, requester),
    }


@plugin.rule('.*')
@plugin.priority('low')
@plugin.thread(True)
def handle_pm(bot, trigger):
    """Route incoming PM replies to the appropriate conversation handler."""
    if not trigger.is_privmsg:
        return
    nick = str(trigger.nick)
    state = _pm_convos.get(nick)
    if not state:
        return
    message = trigger.group(0)
    if state['state'] == 'awaiting_text':
        handle_text_entry(bot, nick, state['channel'], message)
    elif state['state'] == 'awaiting_segment':
        handle_segment_selection(bot, nick, message)
    elif state['state'] == 'awaiting_revert':
        handle_revert_selection(bot, nick, message)
