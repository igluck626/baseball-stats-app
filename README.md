# Baseball Stats App

A web application for tracking and analyzing baseball statistics.

## Project Structure

```
baseball-stats-app/
└── backend/
    ├── api/         # FastAPI route handlers and request/response models
    ├── database/    # Database models, migrations, and connection setup
    └── scripts/     # Utility scripts for data ingestion and maintenance
```

## Backend

Built with **Python** and **FastAPI**.

### Getting Started

1. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. Install dependencies:
   ```bash
   pip install -r backend/requirements.txt
   ```

3. Run the development server:
   ```bash
   uvicorn backend.api.main:app --reload
   ```

## Features

- Player and team statistics
- Game-by-game results
- Season and career stat aggregations
- REST API for querying baseball data
