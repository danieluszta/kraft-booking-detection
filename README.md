# Booking Detection Pipeline + Elastic Leads Agent

Detect whether activity providers (tour operators, adventure companies, experience hosts) have online booking capability on their websites. Ships with two tools and 500 pre-analyzed sample leads.

## What's Inside

### 1. Elastic Leads Agent (free, no API keys)
A Claude Code agent that finds activity providers without booking pages — hot leads for booking platform sales. Uses Claude Code's built-in WebSearch and WebFetch, so it costs nothing to run.

### 2. 9-Pass Booking Detection Pipeline
A full-stack pipeline that progressively analyzes websites: homepage scraping → regex pattern matching → LLM classification → deep browser validation. Achieves ~95% accuracy at ~$0.001/domain.

### 3. 500 Sample Leads
Pre-analyzed results from the activity tourism vertical, including booking status, detected platform, and reasoning.

---

## Quick Start: Elastic Leads Agent

No API keys needed. Just Claude Code.

```bash
# Copy the agent to any project directory
cp -r elastic-leads-agent/ ~/my-project/

# Open Claude Code in that directory
cd ~/my-project && claude

# Then prompt it:
# "Find surf schools in Bali that don't have their own booking page"
# "Prospect adventure activity providers in Costa Rica"
# "Process providers.csv and check which ones have booking pages"
```

The agent works in two modes:
- **Discovery Mode**: Give it a region/activity type, it finds providers on Viator/GetYourGuide/etc. and checks their websites
- **Batch Mode**: Give it a CSV of domains, it processes them in batches of 15-20

---

## Quick Start: 9-Pass Pipeline

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set up API keys

```bash
cp .env.example .env
# Edit .env with your API keys
```

### 3. Run the pipeline

```bash
# Run on the sample data (500 domains)
python3 run_pipeline.py --input sample_data/leads_input.csv --output results.csv

# Run with verbose logging
python3 run_pipeline.py --input sample_data/leads_input.csv --output results.csv --verbose

# Include Linkup deep validation (pass 9, $0.05/domain)
python3 run_pipeline.py --input sample_data/leads_input.csv --output results.csv --include-linkup
```

### Input CSV format

Your input CSV needs a `domain` column:

```csv
domain
example-tours.com
adventure-co.com
pro-diving.com
```

### Output CSV format

```csv
domain,has_booking,booking_platform,reasoning,source_pass
example-tours.com,true,FareHarbor,FareHarbor widget detected on homepage,regex_homepage
adventure-co.com,false,,No booking detected after all passes,no_booking
pro-diving.com,true,,Custom booking form with date picker and payment,llm_crawled
```

---

## The 9 Passes

The pipeline uses progressive narrowing — fast and cheap methods first, expensive ones last. Each pass only processes domains not yet resolved.

| Pass | Method | What It Does | Cost |
|------|--------|-------------|------|
| 1 | Firecrawl Scrape | Scrape homepage HTML + markdown | ~$0.001/domain |
| 2 | LLM (GPT-4o-mini) | Classify homepage HTML | ~$0.0006/domain |
| 3 | Regex | Scan homepage for 30 known booking platform signatures | Free |
| 4 | Firecrawl Crawl | Crawl /book*, /reserv*, /ticket* subpages (max 20 pages) | ~$0.003/domain |
| 5 | Regex | Scan crawled subpages for booking signatures | Free |
| 6 | Firecrawl Crawl | Broad crawl with no path filter (max 25 pages) | ~$0.003/domain |
| 7 | Regex | Scan broad crawl results | Free |
| 8 | LLM (GPT-4o-mini) | Classify concatenated crawl markdown | ~$0.001/domain |
| 9 | Linkup Deep (opt) | Live browser validation for remaining domains | $0.05/domain |

**Typical results on 500 domains:** ~80% have booking, ~20% don't. Total cost ~$1.50 (without Linkup).

---

## API Keys Required

| Service | Required | Cost | What For |
|---------|----------|------|----------|
| [Firecrawl](https://firecrawl.dev) | Yes | ~$16/mo (3k credits) | Scraping and crawling websites (passes 1, 4, 6) |
| [OpenAI](https://platform.openai.com) | Yes | ~$5 per 8k domains | GPT-4o-mini classification (passes 2, 8) |
| [Linkup](https://linkup.so) | Optional | $0.05/domain | Deep browser validation (pass 9 only) |

---

## Cost Estimates

| Scale | Without Linkup | With Linkup |
|-------|---------------|-------------|
| 100 domains | ~$0.50 | ~$5.50 |
| 500 domains | ~$1.50 | ~$26 |
| 5,000 domains | ~$15 | ~$265 |
| 40,000 domains | ~$56 | ~$2,056 |

Pass 9 (Linkup) is optional and only adds ~3% accuracy improvement. The first 8 passes achieve ~95% accuracy on their own.

---

## Project Structure

```
kraft-booking-detection/
├── run_pipeline.py              # 9-pass pipeline orchestrator
├── elastic-leads-agent/
│   └── CLAUDE.md                # Claude Code agent instructions
├── lib/
│   ├── firecrawl_local.py       # Firecrawl API wrapper
│   ├── text_scanner_local.py    # Regex pattern matching
│   ├── llm_analysis_local.py    # OpenAI LLM calls
│   └── linkup_local.py          # Linkup deep search
├── configs/
│   ├── booking_fingerprints.yaml  # 30 regex patterns
│   └── prompts/                   # LLM + Linkup prompts
├── sample_data/
│   ├── leads_input.csv            # 500 domains (pipeline input)
│   └── leads_results.csv         # 500 results (full data)
└── data/                          # Intermediate outputs (created at runtime)
```

---

## License

MIT
