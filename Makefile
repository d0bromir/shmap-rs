SHELL := /bin/bash

# Ported from the C++ shmap's Makefile: shmap-specific build/test targets
# now call cargo instead of a C++ compiler, and SHMAP_BIN points at the
# Rust binary instead. The taskset/chrt/time scaffolding and the eval_*
# pipeline (paftools.js mapeval, gen_reads, pauls_experiment) all carry
# over unchanged since none of it is C++-specific. The external-tool
# comparison targets (eval_minimap, eval_mm2, eval_blend, eval_mapquik,
# eval_winnowmap, eval_mashmap1/3, eval_astarix) are left untouched — they
# reference other projects' binaries under ~/libs/... and have nothing to
# do with this port.

SHMAP_BIN := target/release/shmap

MINIMAP_BIN = ~/libs/minimap2/minimap2
MM2_BIN = ~/libs/mm2-fast/minimap2
BLEND_BIN = ~/libs/blend/bin/blend
MAPQUIK_BIN = ~/libs/mapquik/target/release/mapquik
MERYL_BIN = ~/libs/Winnowmap/bin/meryl
WINNOWMAP_BIN = ~/libs/Winnowmap/bin/winnowmap
MASHMAP1_BIN = ~/libs/mashmap
MASHMAP3_BIN = ~/libs/MashMap/build/bin/mashmap
ASTARIX_BIN = ~/libs/astarix/release/astarix
SURVIVOR_BIN = ~/libs/SURVIVOR/Debug/SURVIVOR

INF = 999999

SHMAP_NOINDEX?=0
SHMAP_ARGS?=

REFNAME ?= chm13-chr1
READSIM_REFNAME ?= $(REFNAME)
ACCURACY ?= ?
DEPTH ?= 1
MEANLEN ?= ?

K ?= 25
R ?= 0.01
T ?= 0.4
MIN_DIFF ?= 0.075
MAX_OVERLAP ?= 0.3
METRIC ?= Containment

M ?= 100000
S ?= 30000

PAUL_THETAS = 1.0 0.9 0.8 0.7 0.6 0.5 0.4 0.3

# eval sketching
Ks = 17 19 21 23 25 27 29 31 33
Rs = 0.001 0.005 0.01 0.05
# eval SH
THETAS = 0.95 0.9 0.85 0.8 0.75 0.7 0.65 0.6 0.55 0.5 0.45 0.4 0.35
METRICS = Containment Jaccard bucket_SH bucket_LCS
# eval MAPQ
MIN_DIFFS = 0.025 0.05 0.075 0.1 0.125 0.15 0.175 0.2 0.225 0.25
MAX_OVERLAPS = 0.1 0.2 0.3 0.4 0.5 0.6 0.7 0.8 0.9 1.0

MAX_SEEDS = 10 30 100 300 1000 3000 10000
MAX_MATCHES = 100 300 1000 3000 10000 30000 100000 300000

K_SLOW ?= $(K)
R_SLOW ?= $(R)
T_SLOW ?= $(T)

DIR = test
REF_DIR = $(DIR)/refs
REF = $(REF_DIR)/$(REFNAME).fa
READSIM_REF = $(REF_DIR)/$(READSIM_REFNAME).fa
READS_PREFIX ?= $(REFNAME)-reads$(READSIM_REFNAME)-a$(ACCURACY)-d$(DEPTH)-l$(MEANLEN)
ALLREADS_DIR = $(DIR)/reads
READS = $(ALLREADS_DIR)/$(READS_PREFIX).fa
ONE_READ = $(ALLREADS_DIR)/$(READS_PREFIX).oneread.fa
ALLOUT_DIR ?= $(DIR)/out
OUTDIR = $(ALLOUT_DIR)/$(READS_PREFIX)

REAL_READS = $(ALLREADS_DIR)/HG002.fq
PBSIM3     = ~/libs/pbsim3/src/pbsim
PAFTOOLS   = ext/paftools.js
SEQKIT     = ~/miniconda3/bin/seqkit

STREAM_LIFT_FASTA = $(DIR)/stream_fasta.py
CHAIN_FILE = $(DIR)/refs/hg002v1.1_to_CHM13v2.0.chain

SHMAP_PREF        = $(DIR)/out/shmap-k$(K)-r$(R)-t$(T)-d$(MIN_DIFF)-o$(MAX_OVERLAP)-m$(METRIC)
MINIMAP_PREF      = $(ALLOUT_DIR)/minimap/$(READS_PREFIX)/minimap
MM2_PREF          = $(ALLOUT_DIR)/mm2-fast/$(READS_PREFIX)/mm2-fast
BLEND_PREF        = $(ALLOUT_DIR)/blend/$(READS_PREFIX)/blend
MAPQUIK_PREF      = $(ALLOUT_DIR)/mapquik/$(READS_PREFIX)/mapquik
WINNOWMAP_PREF    = $(ALLOUT_DIR)/winnowmap/$(READS_PREFIX)/winnowmap
MASHMAP1_PREF     = $(ALLOUT_DIR)/mashmap/$(READS_PREFIX)/mashmap
MASHMAP3_PREF     = $(ALLOUT_DIR)/mashmap3/$(READS_PREFIX)/mashmap3
ASTARIX_PREF      = $(ALLOUT_DIR)/astarix/$(READS_PREFIX)/astarix
SHMAP_NOPRUNE_PREF  = $(ALLOUT_DIR)/shmap-noprune/$(READS_PREFIX)/shmap-noprune
SHMAP_ONESWEEP_PREF = $(ALLOUT_DIR)/shmap-onesweep/$(READS_PREFIX)/shmap-onesweep

TIME_CMD = /usr/bin/time -f "%U\t%M"

.PHONY: all build debug test clean remake \
	simulate_SVs gen_reads eval_sketching eval_thinning fdr_per_theta \
	eval_shmap eval_shmap_noprune eval_shmap_onesweep \
	eval_mashmap1 eval_mashmap3 eval_astarix eval_winnowmap eval_minimap eval_mm2 eval_blend eval_mapquik \
	eval_tools eval_tools_on_datasets eval_shmap_on_datasets eval_shmap_on_datasets_on_metrics \
	pauls_experiment clean_evals

all: build

build: $(SHMAP_BIN)

$(SHMAP_BIN):
	cargo build --release

debug:
	cargo build

test:
	cargo test

remake: clean all

clean:
	cargo clean

simulate_SVs:
	cd $(REF_DIR);\
	$(SURVIVOR_BIN) simSV $(REFNAME).fa SURVIVOR.params 0 0 $(REFNAME)-SVs;\
	mv $(REFNAME)-SVs.fasta $(REFNAME)-SVs.fa

gen_reads:
ifeq ($(wildcard $(READS)),)
	echo $(READS)
	mkdir -p $(ALLREADS_DIR)
	sed -i 's/^\(>[^[:space:]]*\).*/\1/' $(READSIM_REF)

	head -n 400000 $(REAL_READS) >$(REAL_READS).tmp
	$(PBSIM3) --strategy wgs --method sample --sample $(REAL_READS).tmp --genome $(READSIM_REF) --depth $(DEPTH) --prefix $(READS_PREFIX) --no-fastq 1

	$(SEQKIT) faidx $(READSIM_REF)
	$(PAFTOOLS) pbsim2fq $(READSIM_REF).fai "$(READS_PREFIX)"_*.maf >$(READS).unshuf
	$(SEQKIT) shuffle -2 $(READS).unshuf -o $(READS)
	rm -f "$(READS_PREFIX)"_*.maf "$(READS_PREFIX)"_*.ref "$(READS_PREFIX)"_*.fastq $(REAL_READS).tmp

	@if [ "$(READSIM_REFNAME)" != "$(REFNAME)" ]; then \
		echo "Lifting over reads from $(READSIM_REFNAME) to $(REFNAME)"; \
		python $(STREAM_LIFT_FASTA) $(READS) $(CHAIN_FILE) >$(READS).lifted; \
		mv $(READS).lifted $(READS); \
	else \
		echo "READSIM_REFNAME and REFNAME are the same: $(REFNAME). No liftover required."; \
	fi
endif
ifeq ($(wildcard $(ONE_READ)),)
	head -n 2 $(READS) >$(ONE_READ)
endif

eval_sketching: gen_reads
	DIR=$(OUTDIR)/eval_sketching; \
	mkdir -p $${DIR}; \
	for k in $(Ks); do \
		for r in $(Rs); do \
			echo "eval_sketching: $${k} $${r}"; \
			make eval_shmap ALLOUT_DIR=$(DIR)/out_small/sketching K=$${k} R=$${r}; \
		done \
    done

eval_thetas: gen_reads
	DIR=$(OUTDIR)/eval_mapq; \
	mkdir -p $${DIR}; \
	for T in $(THETAS); do \
		echo "eval_params: $${T}"; \
		make eval_shmap ALLOUT_DIR=$(DIR)/out_small/params T=$${T}; \
    done

eval_min_diffs: gen_reads
	DIR=$(OUTDIR)/eval_mapq; \
	mkdir -p $${DIR}; \
	for MIN_DIFF in $(MIN_DIFFS); do \
		echo "eval_min_diffs: $${MIN_DIFF}"; \
		make eval_shmap ALLOUT_DIR=$(DIR)/out_small/min_diffs MIN_DIFF=$${MIN_DIFF}; \
	done

eval_max_overlaps: gen_reads
	DIR=$(OUTDIR)/eval_mapq; \
	mkdir -p $${DIR}; \
	for MAX_OVERLAP in $(MAX_OVERLAPS); do \
		echo "eval_max_overlaps: $${MAX_OVERLAP}"; \
		make eval_shmap ALLOUT_DIR=$(DIR)/out_small/max_overlaps MAX_OVERLAP=$${MAX_OVERLAP}; \
	done

eval_sketching_on_datasets:
	make eval_sketching ALLOUT_DIR=$(DIR)/out_small	REFNAME=chm13   READSIM_REFNAME=hg002   DEPTH=0.1

eval_thinning: build gen_reads
	@DIR=$(OUTDIR)/thinning; \
	mkdir -p $${DIR}; \
	for s in $(MAX_SEEDS); do \
		for m in $(MAX_MATCHES); do \
			f=$${DIR}/"sweepmap-S$${s}-M$${m}"; \
			echo "Processing $${f}"; \
			$(TIME_CMD) -o $${f}.index.time $(SHMAP_BIN) -s $(REF) -p $(ONE_READ) -x -t $(T) -k $(K) -r $(R) -S $${s} -M $${m} 2>&1 >/dev/null; \
			$(TIME_CMD) -o $${f}.time $(SHMAP_BIN) -s $(REF) -p $(READS) -z $${f}.params -x -t $(T) -k $(K) -r $(R) -S $${s} -M $${m} 2> >(tee $${f}.log) >$${f}.paf; \
			-paftools.js mapeval $${f}.paf | tee $${f}.eval; \
		done \
    done

fdr_per_theta: $(SHMAP_BIN) gen_reads
	@DIR=$(OUTDIR)/fdr_per_theta; \
	mkdir -p $${DIR}; \
	for t in $(THETAS); do \
		f=$${DIR}/"shmap-T$${t}"; \
		echo "Processing $${f}"; \
		$(TIME_CMD) -o $${f}.time $(SHMAP_BIN) -s $(REF) -p $(READS) -z $${f}.params -x -t $${t} -k $(K) -r $(R) 2> >(tee $${f}.log) >$${f}.paf; \
		-paftools.js mapeval $${f}.paf | tee $${f}.eval; \
	done

eval_shmap: $(SHMAP_BIN) gen_reads
	@mkdir -p $(shell dirname $(SHMAP_PREF))
	sudo $(TIME_CMD) -o $(SHMAP_PREF).time bash -c '\
		taskset -c 0 chrt -f 99 $(SHMAP_BIN) \
		-s $(REF) -p $(READS) -z $(SHMAP_PREF).params \
		-k $(K) -r $(R) -t $(T) -d $(MIN_DIFF) -o $(MAX_OVERLAP) -m $(METRIC) $(SHMAP_ARGS) \
		2> >(tee $(SHMAP_PREF).log >&2) > $(SHMAP_PREF).paf'
	$(PAFTOOLS) mapeval -r 0.1 $(SHMAP_PREF).paf 2>/dev/null | tee $(SHMAP_PREF).eval || true
	$(PAFTOOLS) mapeval -r 0.1 -Q 0 $(SHMAP_PREF).paf > $(SHMAP_PREF).wrong 2>/dev/null || true
	cat $(SHMAP_PREF).time

eval_shmap_noprune: $(SHMAP_BIN) gen_reads
	@mkdir -p $(shell dirname $(SHMAP_NOPRUNE_PREF))
	$(TIME_CMD) -o $(SHMAP_NOPRUNE_PREF).index.time $(SHMAP_BIN) -s $(REF) -p $(ONE_READ) -k $(K) -r $(R) -t $(T) -M $(M) -x -b 2>/dev/null >/dev/null
	$(TIME_CMD) -o $(SHMAP_NOPRUNE_PREF).time $(SHMAP_BIN) -s $(REF) -p $(READS) -z $(SHMAP_NOPRUNE_PREF).params -k $(K) -r $(R) -t $(T) -M $(M) -x -b    2> >(tee $(SHMAP_NOPRUNE_PREF).log) > $(SHMAP_NOPRUNE_PREF).paf
	-$(PAFTOOLS) mapeval -r 0.1 $(SHMAP_NOPRUNE_PREF).paf | tee $(SHMAP_NOPRUNE_PREF).eval
	-$(PAFTOOLS) mapeval -r 0.1 -Q 0 $(SHMAP_NOPRUNE_PREF).paf >$(SHMAP_NOPRUNE_PREF).wrong

eval_shmap_onesweep: $(SHMAP_BIN) gen_reads
	@mkdir -p $(shell dirname $(SHMAP_ONESWEEP_PREF))
	$(TIME_CMD) -o $(SHMAP_ONESWEEP_PREF).index.time $(SHMAP_BIN) -s $(REF) -p $(ONE_READ) -k $(K) -r $(R) -t $(T) -M $(M) -x -B 2>/dev/null >/dev/null
	$(TIME_CMD) -o $(SHMAP_ONESWEEP_PREF).time $(SHMAP_BIN) -s $(REF) -p $(READS) -z $(SHMAP_ONESWEEP_PREF).params -k $(K) -r $(R) -t $(T) -M $(M) -x -B    2> >(tee $(SHMAP_ONESWEEP_PREF).log) > $(SHMAP_ONESWEEP_PREF).paf
	-$(PAFTOOLS) mapeval -r 0.1 $(SHMAP_ONESWEEP_PREF).paf | tee $(SHMAP_ONESWEEP_PREF).eval
	-$(PAFTOOLS) mapeval -r 0.1 -Q 0 $(SHMAP_ONESWEEP_PREF).paf >$(SHMAP_ONESWEEP_PREF).wrong

# --- External-tool comparison targets: unchanged, not part of this port ---

eval_winnowmap: gen_reads
	@mkdir -p $(shell dirname $(WINNOWMAP_PREF))
	if [ ! -f $(REF_DIR)/winnowmap_$(REF)_repetitive_k15.txt ]; then \
		$(MERYL_BIN) count k=15 output merylDB $(REF); \
		$(MERYL_BIN) print greater-than distinct=0.9998 merylDB > $(REF_DIR)/winnowmap_$(REFNAME)_repetitive_k15.txt; \
	fi
	$(TIME_CMD) -o $(WINNOWMAP_PREF).index.time $(WINNOWMAP_BIN) -W $(REF_DIR)/winnowmap_$(REFNAME)_repetitive_k15.txt -x map-pb -t 1 --secondary=no --sv-off $(REF) $(ONE_READ) >/dev/null 2>/dev/null
	$(TIME_CMD) -o $(WINNOWMAP_PREF).time $(WINNOWMAP_BIN) -W $(REF_DIR)/winnowmap_$(REFNAME)_repetitive_k15.txt -x map-pb -t 1 --secondary=no --sv-off -M 0 --hard-mask-level $(REF) $(READS) 2> >(tee $(WINNOWMAP_PREF).log) >$(WINNOWMAP_PREF).paf
	-$(PAFTOOLS) mapeval $(WINNOWMAP_PREF).paf | tee $(WINNOWMAP_PREF).eval

eval_mashmap1: gen_reads
	@mkdir -p $(shell dirname $(MASHMAP1_PREF))
	$(TIME_CMD) -o $(MASHMAP1_PREF).index.time $(MASHMAP1_BIN) -s $(REF) -q $(ONE_READ) -o $(MASHMAP1_PREF).paf >/dev/null 2>/dev/null
	$(TIME_CMD) -o $(MASHMAP1_PREF).time $(MASHMAP1_BIN)       -s $(REF) -q $(READS)    -o $(MASHMAP1_PREF).paf

eval_mashmap3: gen_reads
	@mkdir -p $(shell dirname $(MASHMAP3_PREF))
	$(TIME_CMD) -o $(MASHMAP3_PREF).index.time $(MASHMAP3_BIN) -t 1 --noSplit --pi 90 -r $(REF) -q $(ONE_READ) -o $(MASHMAP3_PREF).paf >/dev/null 2>/dev/null
	$(TIME_CMD) -o $(MASHMAP3_PREF).time $(MASHMAP3_BIN)       -t 1 --noSplit --pi 90 -r $(REF) -q $(READS)    -o $(MASHMAP3_PREF).paf
	-$(PAFTOOLS) mapeval $(MASHMAP3_PREF).paf | tee $(MASHMAP3_PREF).eval

eval_astarix: gen_reads
	@mkdir -p $(shell dirname $(ASTARIX_PREF))
	$(TIME_CMD) -o $(ASTARIX_PREF).index.time $(ASTARIX_BIN) align-optimal -g $(REF) -q $(ONE_READ) -o $(ASTARIX_PREF).paf >/dev/null 2>/dev/null
	$(TIME_CMD) -o $(ASTARIX_PREF).time $(ASTARIX_BIN)       align-optimal -g $(REF) -q $(READS)    -o $(ASTARIX_PREF).paf
	-$(PAFTOOLS) mapeval $(ASTARIX_PREF).paf | tee $(ASTARIX_PREF).eval

eval_minimap: gen_reads
	@mkdir -p $(shell dirname $(MINIMAP_PREF))
	$(TIME_CMD) -o $(MINIMAP_PREF).index.time $(MINIMAP_BIN) -x map-hifi -t 1 --secondary=no -M 0 --hard-mask-level $(REF) $(ONE_READ) >/dev/null 2>/dev/null
	$(TIME_CMD) -o $(MINIMAP_PREF).time       $(MINIMAP_BIN) -x map-hifi -t 1 --secondary=no -M 0 --hard-mask-level $(REF) $(READS) 2> >(tee $(MINIMAP_PREF).log) >$(MINIMAP_PREF).paf
	-$(PAFTOOLS) mapeval $(MINIMAP_PREF).paf | tee $(MINIMAP_PREF).eval
	$(PAFTOOLS) mapeval -r 0.1 -Q 0 $(MINIMAP_PREF).paf >$(MINIMAP_PREF).wrong 2>/dev/null || true

eval_mm2: gen_reads
	@mkdir -p $(shell dirname $(MM2_PREF))
	$(TIME_CMD) -o $(MM2_PREF).index.time $(MM2_BIN) -x map-hifi -t 1 --secondary=no -M 0 --hard-mask-level -a $(REF) $(ONE_READ) >/dev/null 2>/dev/null
	$(TIME_CMD) -o $(MM2_PREF).time       $(MM2_BIN) -x map-hifi -t 1 --secondary=no -M 0 --hard-mask-level $(REF) $(READS) 2> >(tee $(MM2_PREF).log) >$(MM2_PREF).paf
	-$(PAFTOOLS) mapeval $(MM2_PREF).paf | tee $(MM2_PREF).eval

eval_blend: gen_reads
	@mkdir -p $(shell dirname $(BLEND_PREF))
	$(TIME_CMD) -o $(BLEND_PREF).index.time $(BLEND_BIN) -x map-hifi -t 1 -N 0 $(REF) $(ONE_READ) >/dev/null 2>/dev/null
	$(TIME_CMD) -o $(BLEND_PREF).time $(BLEND_BIN) -x map-hifi -t 1 --secondary=no -M 0 --hard-mask-level $(REF) $(READS) 2> >(tee $(BLEND_PREF).log) >$(BLEND_PREF).paf
	-$(PAFTOOLS) mapeval $(BLEND_PREF).paf | tee $(BLEND_PREF).eval

eval_mapquik: gen_reads
	@mkdir -p $(shell dirname $(MAPQUIK_PREF))
	$(TIME_CMD) -o $(MAPQUIK_PREF).index.time $(MAPQUIK_BIN) $(ONE_READ) --reference $(REF) --threads 1  >/dev/null
	$(TIME_CMD) -o $(MAPQUIK_PREF).time $(MAPQUIK_BIN) $(READS) --reference $(REF) --threads 1 -p $(MAPQUIK_PREF) | tee $(MAPQUIK_PREF).log
	-$(PAFTOOLS) mapeval $(MAPQUIK_PREF).paf | tee $(MAPQUIK_PREF).eval

# --- End external-tool targets ---

eval_tools: eval_mashmap3

eval_tools_on_datasets:
	make eval_tools ALLOUT_DIR=$(DIR)/out_small REFNAME=chm13   READSIM_REFNAME=chm13   DEPTH=1
	make eval_tools ALLOUT_DIR=$(DIR)/out_small REFNAME=chm13   READSIM_REFNAME=hg002   DEPTH=1
	make eval_tools ALLOUT_DIR=$(DIR)/out_small REFNAME=chm13   READS_PREFIX=HG002_small

eval_shmap_on_datasets:
	make eval_shmap ALLOUT_DIR=$(DIR)/out_small REFNAME=chrY 	READSIM_REFNAME=chrY 	DEPTH=2
	make eval_shmap ALLOUT_DIR=$(DIR)/out_small REFNAME=chm13 	READSIM_REFNAME=hg002 	DEPTH=1
	make eval_shmap ALLOUT_DIR=$(DIR)/out_small REFNAME=chm13   READSIM_REFNAME=chm13   DEPTH=1
	make eval_shmap ALLOUT_DIR=$(DIR)/out_small REFNAME=chm13   READS_PREFIX=HG002_small

eval_shmap_on_datasets_on_metrics:
	make eval_shmap_on_datasets METRIC=bucket_SH
	make eval_shmap_on_datasets METRIC=bucket_LCS
	make eval_shmap_on_datasets METRIC=fixed_C

pauls_experiment: $(SHMAP_BIN) gen_reads
	@DIR=$(ALLOUT_DIR)/pauls_experiment; \
	for t in $(PAUL_THETAS); do \
		pref=$${DIR}/"T$${t}"; \
		make eval_shmap ALLOUT_DIR=$${pref} T=$${t} REFNAME=t2tChrY DEPTH=0.1; \
		make eval_shmap ALLOUT_DIR=$${pref} T=$${t} REFNAME=chm13   DEPTH=0.01; \
		make eval_shmap ALLOUT_DIR=$${pref} T=$${t} REFNAME=t2tChrY DEPTH=0.1  MEANLEN=24000; \
	done

clean_evals:
	rm -r $(ALLREADS_DIR)
	rm -r $(ALLOUT_DIR)
