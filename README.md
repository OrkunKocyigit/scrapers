# FC2PPV-DB Stash Scraper

A [Stash](https://stashapp.cc) metadata provider for **https://fc2ppv-db.com** —
an FC2 PPV metadata database. It scrapes scenes and actresses through a local
[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) instance because the
site sits behind Cloudflare and an age verification wall.

> **Note:** this scraper targets `fc2ppv-db.com` (**with a hyphen**).
> `fc2ppvdb.com` (no hyphen) is a different website with its own community
> scraper — that one is installed under the scraper id `fc2ppvdb`. Do not
> confuse the two; this scraper's id is `fc2ppv-db`.

## Supported operations

| Stash flow | Script operation | What it does |
| --- | --- | --- |
| Scrape with URL (scene) | `scene-by-url` | Reads a `fc2ppv-db.com/.../videos/{id}` URL and returns one scene: code `FC2-PPV-{id}`, title, release date, seller as studio, actresses as performers (with face image when the page has one), thumbnail, `/en` URL and duration details. |
| Scrape (scene fragment) | `scene-by-fragment` | Resolves the FC2 id from the fragment's URLs, file paths or title and returns that video's metadata. Unresolvable fragments return `{}` instead of an error. |
| Scrape with query fragment | `scene-by-query-fragment` | Same as `scene-by-fragment`. |
| Scene by name / search | `scene-by-name` | Parses `/en/search?q=` results into `{title, url}` candidates. A name that already contains an FC2 id returns a single candidate without any network fetch. |
| Scrape with URL (performer) | `performer-by-url` | Reads an actress page (`/en|ja|zh/actresses/{uuid}`) and returns name, URL and face image from the page's JSON-LD `Person` block (H1 fallback). |
| Scrape (performer fragment) | `performer-by-fragment` | Delegates to `performer-by-url` when the fragment carries a `url` (or `urls[0]`). The site has no performer name search, so a fragment without a URL exits with an error. |

## Requirements

- Python **3.10+** (standard library only — no pip packages)
- FlareSolverr reachable from the machine running Stash

## Install via source index (recommended)

This repository is shaped as a Stash scraper source, so installation is:

1. Push this repository to GitHub with the branch named `main`.
2. In the repository: **Settings → Pages → Build and deployment → Source = GitHub Actions**.
   The workflow in `.github/workflows/deploy.yml` runs on every push that touches
   `scrapers/**` and publishes a generated `index.yml` together with the scraper zip.
3. Wait for the "Deploy repository to Github Pages" workflow to finish, then verify the
   index is reachable at:

   ```
   https://orkunkocyigit.github.io/scrapers/main/index.yml
   ```

4. In Stash: **Settings → Metadata Providers → Add Source**, give it a name and paste that
   index URL. Stash downloads and installs `fc2ppv-db` from it.
5. The provider appears in the scraper list as **FC2PPV-DB**; use it from the normal
   scraping flows (scrape a scene/performer by URL, query fragment, or name).

## Manual install (no GitHub Pages)

1. Copy the contents of `scrapers/fc2ppv-db/` into a Stash scraper directory:
   - Windows: `%USERPROFILE%\.stash\scrapers\fc2ppv-db\`
   - Linux/macOS: `~/.stash/scrapers/fc2ppv-db/`

   Make sure both `fc2ppv-db.py` and `fc2ppv-db.yml` end up in that folder.
2. In Stash: **Settings → Metadata Providers → Reload Scrapers**.

## FlareSolverr

The scraper sends every page request through FlareSolverr with the site's
`age-verified=true` cookie. A quick Docker setup:

```sh
docker run -d --name flaresolverr -p 8191:8191 -e LOG_LEVEL=info --restart unless-stopped ghcr.io/flaresolverr/flaresolverr:latest
```

## Configuration

The FlareSolverr endpoint is resolved in this order:

1. `FLARESOLVERR_URL` environment variable (highest precedence)
2. `flaresolverr_url` in `fc2ppv-db.ini`, placed **next to `fc2ppv-db.py`**
3. Default: `http://localhost:8191/v1`

Example `fc2ppv-db.ini`:

```ini
[DEFAULT]
flaresolverr_url = http://localhost:8191/v1
```

## Running the tests

```sh
python -m unittest discover -s tests -v
```

The suite is fully offline: it never contacts fc2ppv-db.com, FlareSolverr or any other
host (HTTP calls are patched and HTML fixtures under `tests/fixtures/` are used). The
`tests/` directory lives outside `scrapers/` on purpose, so it is not packaged into the
published scraper zip.

## Limitations

- **No performer name search.** fc2ppv-db.com has no actress search page, so
  `performer-by-fragment` only works when the fragment carries a URL.
- **No bio, birthdate or aliases.** The site does not expose them; the scraper does not
  invent fields.
- **No tags.** The site has no tag links.
- **Cloudflare ASN block (error 1005).** Some networks/networks' ASNs are blocked by
  Cloudflare; the scraper reports this clearly but cannot work around it.
- **Links use the `/en` locale.** Canonical scene/actress URLs are emitted with `/en`.
- **FlareSolverr must be running** and adds render latency to every scrape.
- **Fragment operations need the id** in the file name, URL or title
  (e.g. `FC2-PPV-4985048.mp4`, `fc2ppv4985048`); without a resolvable id they return `{}`.
- **Site markup changes can break extraction.** The selectors are grounded in fixture
  tests; if the site changes its DOM, update `parse_video_page`/`parse_actress_page` and
  the fixtures.

## Licence

AGPL-3.0 — see [LICENCE](LICENCE).
