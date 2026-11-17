# JobRadar

JobRadar finds new software engineering jobs for you. It checks thousands of company
career pages (Greenhouse, Lever, Ashby, Workday and more) and filters out roles that
don't fit. Then it uses Claude to score the rest against your resume and sends the
best matches to Telegram.

## What you need

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- [Docker](https://www.docker.com/) (runs the local database)
- Optional: an [Anthropic API key](https://console.anthropic.com) for AI matching,
  and a Telegram bot for alerts

## Setup

**1. Install**

```bash
git clone https://github.com/sevara-badriddinova/jobradar.git
cd jobradar
uv sync
```

**2. Start the database**

```bash
docker compose up -d
uv run jobradar init-db
```

**3. Add your keys**

```bash
cp .env.example .env          # fill in the keys you want to use
set -a; source .env; set +a   # run this in every new terminal
```

`.env` is git-ignored. Never commit it or paste keys into other files.

**4. Find jobs**

```bash
uv run jobradar companies sync companies.toml
uv run jobradar collect       # fetch postings
uv run jobradar filter        # drop roles that don't fit your filters
uv run jobradar new --hours 24
```

To change the filters (location, years of experience, title keywords), edit
`profile.toml`. Then run `uv run jobradar filter --recheck`.

**5. Match jobs to your resume** (needs `ANTHROPIC_API_KEY`)

```bash
uv run jobradar profile extract path/to/resume.pdf
uv run jobradar match
uv run jobradar top           # best matches first
```

**6. Get alerts on Telegram** (optional)

Create a bot with [@BotFather](https://t.me/BotFather) and put its token in `.env`.
Send your bot a message. Then run `uv run jobradar telegram setup` to get your chat ID
and add it to `.env` as well. After that:

```bash
uv run jobradar telegram setup --test   # sends a test message
uv run jobradar notify                  # sends new matches
uv run jobradar telegram listen         # keep running so buttons respond
```

## Run the tests

```bash
uv run pytest -m unit
```

## More guides

- [Telegram](docs/08-telegram.md)
- [Tracking applications from Gmail](docs/09-applications.md) or [Notion](docs/11-notion.md)
- [Running it in the cloud](docs/18-google-vm.md)
- All design notes: [`docs/`](docs/)
