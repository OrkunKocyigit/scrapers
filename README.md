# FC2 Stash Scrapers

Stash metadata providers for FC2 PPV databases, packaged as a single scraper
source:

| Scraper id | Site | Transport | Login |
| --- | --- | --- | --- |
| `fc2ppv-db` | https://fc2ppv-db.com | [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) (Cloudflare + age wall) | not required |
| `fc2cmadb` | https://fc2cmadb.com | plain HTTPS | cookie required for actress pages/search |

> **Note:** `fc2ppv-db.com` (**with a hyphen**) is the target of the `fc2ppv-db`
> scraper. `fc2ppvdb.com` (no hyphen) is a different website with its own
> community scraper — that one is installed under the scraper id `fc2ppvdb`. Do
> not confuse the two.

## Supported operations

### fc2ppv-db (fc2ppv-db.com)

| Stash flow | Script operation | What it does |
| --- | --- | --- |
| Scrape with URL (scene) | `scene-by-url` | Reads a `fc2ppv-db.com/.../videos/{id}` URL and returns one scene: code `FC2-PPV-{id}`, title (omitted when the site has none, so your existing title is kept), release date, seller as studio, actresses as performers (with face image when the page has one), thumbnail, tags, `/en` URL, and details (duration followed by the site's description). |
| Scrape (scene fragment) | `scene-by-fragment` | Resolves the FC2 id from the fragment's URLs, file paths or title and returns that video's metadata. Unresolvable fragments return no result (`null`) instead of an empty object. |
| Scrape with query fragment | `scene-by-query-fragment` | Same as `scene-by-fragment`. |
| Scene by name / search | `scene-by-name` | Parses `/en/search?q=` results into `{title, url}` candidates. A name that already contains an FC2 id returns a single candidate without any network fetch. |
| Scrape with URL (performer) | `performer-by-url` | Reads an actress page (`/en|ja|zh/actresses/{uuid}`) and returns name, URL and face image from the page's JSON-LD `Person` block (H1 fallback). |
| Performer search | `performer-by-name` | Queries the actress search (`/en/actresses?view=all&q=`) and returns `{name, url, image}` candidates from the first page of matches. |
| Scrape (performer fragment) | `performer-by-fragment` | Delegates to `performer-by-url` when the fragment carries a `url` (or `urls[0]`); a fragment without a URL exits with an error — use the performer search to find her first. |

### fc2cmadb (fc2cmadb.com)

| Stash flow | Script operation | What it does |
| --- | --- | --- |
| Scrape with URL (scene) | `scene-by-url` | Reads a `fc2cmadb.com/articles/{id}` URL and returns one scene: code `FC2-PPV-{id}`, title, release date, writer as studio (with image when the site has one), actresses as performers (with aliases), thumbnail, tags, and details (`Duration: X`). |
| Scrape (scene fragment) | `scene-by-fragment` | Resolves the FC2 id from the fragment's URLs, file paths or title and returns that video's metadata. Unresolvable fragments return no result (`null`). |
| Scrape with query fragment | `scene-by-query-fragment` | Same as `scene-by-fragment`. |
| Scene by name / search | `scene-by-name` | Uses the site search (`/search?keyword=…&stype=title`) and returns `{title, url}` candidates from the first page. A name that already contains an FC2 id returns a single candidate without any network fetch. |
| Scrape with URL (performer) | `performer-by-url` | Reads an actress page (`/actresses/{id}`, login required) and returns name, URL and aliases. |
| Performer search | `performer-by-name` | Uses the actress search (`/search?keyword=…&stype=actress`, login required) and returns `{name, url}` candidates from the first page (40 matches). |
| Scrape (performer fragment) | `performer-by-fragment` | Delegates to `performer-by-url` when the fragment carries a `url` (or `urls[0]`); a fragment without a URL exits with an error. |

## Requirements

- Python **3.10+** (standard library only — no pip packages)
- For `fc2ppv-db`: FlareSolverr reachable from the machine running Stash

## Install via source index (recommended)

This repository is shaped as a Stash scraper source, so installation is:

1. Push this repository to GitHub with the branch named `main`.
2. In the repository: **Settings → Pages → Build and deployment → Source = GitHub Actions**.
   The workflow in `.github/workflows/deploy.yml` runs on every push that touches
   `scrapers/**` and publishes a generated `index.yml` together with the scraper zips.
3. Wait for the "Deploy repository to Github Pages" workflow to finish, then verify the
   index is reachable at:

   ```
   https://orkunkocyigit.github.io/scrapers/main/index.yml
   ```

4. In Stash: **Settings → Metadata Providers → Add Source**, give it a name and paste that
   index URL. Stash downloads and installs `fc2ppv-db` and `fc2cmadb` from it.
5. The providers appear in the scraper list as **FC2PPV-DB** and **FC2CMADB**; use them
   from the normal scraping flows (scrape a scene/performer by URL, query fragment, or name).

## Manual install (no GitHub Pages)

1. Copy the contents of `scrapers/fc2ppv-db/` and/or `scrapers/fc2cmadb/` into a Stash
   scraper directory:
   - Windows: `%USERPROFILE%\.stash\scrapers\<scraper-id>\`
   - Linux/macOS: `~/.stash/scrapers/<scraper-id>/`

   Make sure the `.py` and `.yml` files of a scraper end up in the same folder.
2. In Stash: **Settings → Metadata Providers → Reload Scrapers**.

## FlareSolverr (fc2ppv-db only)

The `fc2ppv-db` scraper sends every page request through FlareSolverr with the
site's `age-verified=true` cookie. `fc2cmadb` does not need FlareSolverr. A quick
Docker setup:

```sh
docker run -d --name flaresolverr -p 8191:8191 -e LOG_LEVEL=info --restart unless-stopped ghcr.io/flaresolverr/flaresolverr:latest
```

## Configuration

### fc2ppv-db

All settings are resolved in this order: environment variable, then the key in
`fc2ppv-db.ini` placed **next to `fc2ppv-db.py`**, then the default.

| Setting | Environment variable | INI key | Default |
| --- | --- | --- | --- |
| FlareSolverr endpoint | `FLARESOLVERR_URL` | `flaresolverr_url` | `http://localhost:8191/v1` |
| Session name (browser reuse) | `FLARESOLVERR_SESSION` | `session_name` | `fc2ppv-db` |
| Session TTL (minutes) | `FLARESOLVERR_SESSION_TTL_MINUTES` | `session_ttl_minutes` | `30` |

Example `fc2ppv-db.ini`:

```ini
[DEFAULT]
flaresolverr_url = http://localhost:8191/v1
session_name = fc2ppv-db
session_ttl_minutes = 30
```

#### Session reuse

The scraper reuses a named FlareSolverr session: `sessions.create` returns the
existing session when it is already active, otherwise it launches one. The
Cloudflare challenge is solved once per session lifetime instead of on every
request (about 20s for the first scrape, ~6s for subsequent ones), and the
`age-verified` cookie persists in the browser. Sessions rotate automatically
after `session_ttl_minutes` of age, and a failed request recreates the session
and retries once.

Set `session_name = none` (or `FLARESOLVERR_SESSION=none`) to disable reuse so
every request runs in a temporary browser. Note: FlareSolverr has no background
cleanup, so the named session stays alive until it rotates on the next scrape or
FlareSolverr restarts — destroy it manually with
`{"cmd": "sessions.destroy", "session": "fc2ppv-db"}` if you want it gone sooner.

### fc2cmadb

| Setting | Environment variable | INI key | Default |
| --- | --- | --- | --- |
| Login cookie | `FC2CMADB_COOKIE` | `cookie` | empty |

Example `fc2cmadb.ini` (next to `fc2cmadb.py`):

```ini
[fc2cmadb]
cookie = fc2cmadb-session=…; remember_web_…=…; ageVerified=true
```

#### fc2cmadb login cookie

- Article and writer pages are **public**: scene scraping works without a login.
- Actress pages and actress search return **403** without a login.
- To get the cookie: log in at <https://fc2cmadb.com/login> in your browser, open
  DevTools → **Application → Cookies → https://fc2cmadb.com**, and copy
  `fc2cmadb-session` and `remember_web_*` (or copy the whole `Cookie` request
  header from any request to the site). Add `ageVerified=true` to skip the age
  modal.
- `remember_web_*` is the important one: it re-authenticates even after the
  session cookie expires, and the scraper automatically uses the refreshed
  session issued in the response.
- Keep the INI file private (it is listed in `.gitignore`); it grants full
  access to your account.

## Running the tests

```sh
python -m unittest discover -s tests -v
```

The suite is fully offline: it never contacts fc2ppv-db.com, fc2cmadb.com,
FlareSolverr or any other host (HTTP calls are patched and HTML fixtures under
`tests/fixtures/` are used). The `tests/` directory lives outside `scrapers/` on
purpose, so it is not packaged into the published scraper zips.

## Limitations

### fc2ppv-db

- **Actress search returns the first page.** `performer-by-name` reads up to the first
  page (~24 matches) of the actress search; refine the query if the performer is missing.
- **No bio, birthdate or aliases.** The site does not expose them; the scraper does not
  invent fields.
- **Some videos have no tags.** The site shows tag pills only when a video has them;
  older videos (e.g. `4548515`) return no `tags` key.
- **Cloudflare ASN block (error 1005).** Some networks/networks' ASNs are blocked by
  Cloudflare; the scraper reports this clearly but cannot work around it.
- **Links use the `/en` locale.** Canonical scene/actress URLs are emitted with `/en`.
- **FlareSolverr must be running** and adds render latency to every scrape.
- **Fragment operations need the id** in the file name, URL or title
  (e.g. `FC2-PPV-4985048.mp4`, `fc2ppv4985048`); without a resolvable id they return
  no result (`null`).
- **Site markup changes can break extraction.** The selectors are grounded in fixture
  tests; if the site changes its DOM, update `parse_video_page`/`parse_actress_page` and
  the fixtures.

### fc2cmadb

- **No description.** The site has no description field, so `details` contains only
  `Duration: X`.
- **No actress images.** The site serves a `no-image` placeholder for actresses, so
  performer images are omitted.
- **Actress pages/search need a login.** Without a configured cookie they fail with a
  clear stderr message; scene scraping still works.
- **Search returns the first page** (30 scene results / 40 actress results).
- **Site markup changes can break extraction.** The parsers are grounded in fixture
  tests; if the site changes its page JSON, update `parse_data_page` consumers and the
  fixtures.

### Both

- **Failures surface as "no results".** Stash reads the script's stdout before its exit
  code, so runtime failures (not found, login required, network errors, ...) emit an
  empty result (`null`, or `[]` for the search operations) and log the reason to stderr,
  which Stash shows as `[Scrape / <scraper-id>] ...` in its logs.

## Licence

AGPL-3.0 — see [LICENCE](LICENCE).
