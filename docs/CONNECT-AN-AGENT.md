# Connect Tickover to an AI assistant (optional)

Tickover works fully on its own. Skip this guide unless you want an AI assistant to help plan your days.

When connected, an assistant can read your plan, build tomorrow's study blocks, move unfinished work forward, and check your progress. It does this through Tickover's **API**, which is simply a way for another program to use the app. You control access with a **token**, which works like a password.

## Will it work with my assistant?

The assistant has to be able to reach your copy of Tickover. Tickover runs on your own computer, so:

- **Works:** assistants that run on your computer, can run commands, or can make web requests from your computer (for example terminal-based coding assistants or self-hosted agent apps), and simple scripts.
- **Does not work:** chat websites in a browser (ChatGPT, Claude.ai, Gemini and similar). They live on the internet and cannot see an app on your computer.

If you are not sure, skip this. You lose nothing by using Tickover without it.

## Set up in five steps

1. **Open Tickover.** The desktop app must be open, or your server running. The API only answers while Tickover is running. On the desktop app the address is `http://127.0.0.1:8765`.
2. **Make a token.** Open **Settings**, find **Connect an AI agent**, and click **Generate token**. The token is copied for you and shown only once.
3. **Copy the instructions.** Click **Copy agent instructions**. This copies a ready-made note that tells an assistant the address, how to log in, and how to use the plan. It includes your token if you just made one.
4. **Give it to your assistant.** Paste the note into a conversation with your assistant, or save it in the assistant's own instructions. Then say: *"Use these instructions to work with my Tickover plan. Show me any change before you make it."*
5. **Test it.** Replace `YOUR_TOKEN` and the date, then run the command in a terminal.

   Mac or Linux:
   ```bash
   curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8765/api/v1/agent/plan/2026-11-08
   ```
   Windows (PowerShell):
   ```powershell
   curl.exe -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8765/api/v1/agent/plan/2026-11-08
   ```
   You should see your plan as text starting with `{"day":`. If you do, it works.

## Keep your token safe

- Anyone with the token can read and change your plan. Treat it like a password.
- Only paste it into assistants you trust. If you paste it into an online service, that service receives it.
- To get a new token, click **Make a new token**. The old one stops working at once.
- To end access completely, click **Turn off**.
- If a token is set in the server's `.env` file, Settings cannot change it. Edit the file instead.

## Things to ask your assistant

- "Look at my plan for tomorrow and tell me how much time is free."
- "Plan tomorrow: 4 hours of study with 2 blocks of practice questions. Show me the plan before you apply it."
- "I did not finish everything today. Preview moving it to tomorrow and wait for my approval."
- "How many hours did I focus this week?"

## How changes stay safe

- **Preview first.** Assistants can preview a change without applying it. They should show you what will be added, changed or removed, then apply only after you agree.
- **Revisions.** Every day has a revision number. If you edit your plan while the assistant is working, its change is refused with a `409` message and it must preview again. Nothing gets overwritten silently.
- **Locked tasks.** Tasks you lock are protected from assistant changes.
- **Undo.** A rollover (moving unfinished work to another day) can be undone while the moved tasks are untouched.
- **History.** Tickover keeps a log of what the assistant did.

Before your first try, use **Settings → Download backup** so you can restore if something goes wrong.

## For tinkerers: fill a day with one command

Each task needs a unique `external_id` so Tickover keeps its history when a plan is refreshed. `type` must be one of `study`, `video`, `anki`, `questions`, `build` or `review`, and `duration` is in minutes (1 to 720).

Preview first. This changes nothing:

```bash
curl -X POST http://127.0.0.1:8765/api/v1/agent/plan/2026-11-08/preview \
  -H "Authorization: Bearer YOUR_TOKEN" -H "Content-Type: application/json" \
  -d '{"replace_existing": true, "tasks": [
        {"external_id": "cardio-1", "label": "Cardiology lecture", "subject": "Cardiology", "type": "video", "duration": 45,
         "items": [{"name": "Watch", "min": 30}, {"name": "Make cards", "min": 15}]}]}'
```

Apply it with a `PUT` to the same address without `/preview`. `replace_existing: true` replaces everything planned for that day. Add `"expected_revision": N`, using the revision from reading the day, to avoid overwriting a newer plan.

Every endpoint is listed, with its fields, at `http://127.0.0.1:8765/docs` while Tickover is running.

## If something goes wrong

| What you see | What it means |
|---|---|
| `400` | A required field is missing, for example a task added without an `external_id`. |
| `401` | The token is missing or wrong. Check for extra spaces, or make a new token. |
| "Connection refused" or "could not connect" | Tickover is not running, or the address is wrong. |
| `409` | Your plan changed after the preview. Preview again and re-check. |
| `422` | A field is invalid, for example an unknown `type` or a `duration` over 720. |

## Using it from another device (advanced)

The desktop app only accepts connections from its own computer. To reach Tickover from elsewhere, run the server version on an always-on computer, keep it on a private network such as Tailscale, and set `APP_PASSWORD`. See the main README. Never expose it directly to the internet without a password and HTTPS.
