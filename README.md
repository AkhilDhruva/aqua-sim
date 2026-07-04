# aqua-sim

Water/aquatic simulation project — starter scaffold.

## Ready-to-go instructions

### 1. Clone

```bash
git clone https://github.com/AkhilDhruva/aqua-sim.git
cd aqua-sim
```

### 2. Set up environment (Python 3.10+)

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
```

### 3. Run

```bash
aqua-sim            # runs the simulation entry point
# or
python -m aqua_sim
```

### 4. Test

```bash
pytest
```

## Project layout

```
aqua-sim/
├── src/aqua_sim/       # simulation package
│   ├── __init__.py
│   ├── __main__.py     # python -m aqua_sim
│   └── main.py         # entry point / CLI
├── tests/              # pytest suite
├── pyproject.toml      # packaging + deps
└── README.md
```

## Remote workflow

Work happens on this machine via remote access; commits push to `main`
(or feature branches → PR) on GitHub.

```bash
git pull                     # sync before starting
git checkout -b feature/x    # optional: branch per feature
# ... work ...
git add -A && git commit -m "..."
git push -u origin feature/x
```
