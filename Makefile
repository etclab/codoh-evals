RUN     ?= default
CELLS   ?= default
WORKERS ?= 56
PROGRESS ?= 50
SESSION ?= sweep
OUT      = out/$(RUN)

PY = python3

.PHONY: help smoke sweep run all aggregate heatmap full attach kill log clean

help:
	@echo "Targets:"
	@echo "  smoke               # tmux: 9-cell smoke (B,T_max corners)"
	@echo "  sweep               # tmux: full $(CELLS) preset run"
	@echo "  run                 # tmux: arbitrary preset (CELLS=...)"
	@echo "  all                 # tmux: sweep + aggregate + heatmap, one shot"
	@echo "  aggregate           # lens_a/lens_b CSVs from out/<RUN>"
	@echo "  heatmap             # (B,T_max) heatmap + gradient gate"
	@echo "  full                # aggregate + heatmap"
	@echo "  attach              # tmux attach -t \$$SESSION"
	@echo "  log                 # tail -f out/<RUN>/run.log"
	@echo "  kill                # tmux kill-session -t \$$SESSION"
	@echo "  clean               # rm -rf out/<RUN>"
	@echo ""
	@echo "Vars (override on the command line):"
	@echo "  RUN=$(RUN)  CELLS=$(CELLS)  WORKERS=$(WORKERS)  PROGRESS=$(PROGRESS)  SESSION=$(SESSION)"

smoke: CELLS=smoke
smoke: RUN=smoke
smoke: SESSION=smoke
smoke: run

sweep: run

run:
	@mkdir -p $(OUT)
	@tmux has-session -t $(SESSION) 2>/dev/null && { \
	    echo "tmux session '$(SESSION)' exists — attach or 'make kill SESSION=$(SESSION)'"; exit 1; } || true
	tmux new-session -d -s $(SESSION) \
	    "$(PY) -m sim.cli run --cells $(CELLS) --workers $(WORKERS) \
	         --progress $(PROGRESS) --out-dir $(OUT) \
	         $(EXTRA) 2>&1 | tee $(OUT)/run.log"
	@echo "session=$(SESSION)  out=$(OUT)"
	@echo "attach: tmux attach -t $(SESSION)    log: make log RUN=$(RUN)"

all:
	@mkdir -p $(OUT)
	@tmux has-session -t $(SESSION) 2>/dev/null && { \
	    echo "tmux session '$(SESSION)' exists — attach or 'make kill SESSION=$(SESSION)'"; exit 1; } || true
	tmux new-session -d -s $(SESSION) \
	    "($(PY) -m sim.cli run --cells $(CELLS) --workers $(WORKERS) \
	         --progress $(PROGRESS) --out-dir $(OUT) $(EXTRA) \
	     && $(PY) -m sim.analyze_b --out-dir $(OUT) \
	     && $(PY) -m sim.analyze_a --out-dir $(OUT) \
	     && $(PY) -m sim.plot_heatmap --out-dir $(OUT) --metric top5_acc \
	     ) 2>&1 | tee $(OUT)/run.log; echo '[all done — press enter to close]'; read"
	@echo "session=$(SESSION)  out=$(OUT)"
	@echo "attach: tmux attach -t $(SESSION)    log: make log RUN=$(RUN)"

aggregate:
	$(PY) -m sim.analyze_b --out-dir $(OUT)
	$(PY) -m sim.analyze_a --out-dir $(OUT)

heatmap:
	$(PY) -m sim.plot_heatmap --out-dir $(OUT) --metric top5_acc

full: aggregate heatmap

attach:
	tmux attach -t $(SESSION)

log:
	tail -f $(OUT)/run.log

kill:
	tmux kill-session -t $(SESSION) 2>/dev/null || true

clean:
	rm -rf $(OUT)
