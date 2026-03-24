# topic_updater

A [Sopel](https://sopel.chat/) IRC bot plugin for collaboratively managing channel topics. Quotes from chat can be appended to the topic, topic changes go to a community vote, and full topic history is kept for rollbacks.

## Commands

| Command | Description |
|---------|-------------|
| `.topic_add <nick>` | Append the most recent message from that nick to the topic |
| `.topic_add <nick> <n>` | Append the nth most recent message from that nick |
| `.topic_add <n>` | Append the nth most recent message overall |
| `.topic_add` | Open a PM flow to type topic text manually |
| `.topic_back` | Start a vote to restore the previous topic |
| `.topic_revert` | PM you with recent topic history to pick one to restore |
| `yes` / `no` | Vote during an active poll |

## How It Works

1. `.topic_add` grabs a message from the channel buffer and formats it as `<nick> message`
2. If it fits within the server's `TOPICLEN`, it's appended immediately and saved to `quotes.db`
3. If the topic is full, a PM flow offers replacement options (which existing segment to swap out)
4. The chosen replacement goes to a 2-minute channel vote — majority wins
5. All topic changes are logged to `topic_history.jsonl` for rollback

## Requirements

- Python 3.10+
- Sopel IRC bot
- Bot must have channel op (`@`) to set topics
- `quotes.db` — shared with the `quote` plugin (quotes saved on successful topic update)

## Notes

- Message buffer holds the last 200 lines per channel, excluding bot commands and ignored nicks (`igor`, `ptpinfo`)
- Topic history is stored in `topic_history.jsonl` in Sopel's home directory
- PM conversations time out after 5 minutes of inactivity
- Polls time out after 2 minutes; ties and no-votes leave the topic unchanged
- Only one poll can run per channel at a time
