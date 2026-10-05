#!/bin/bash
# PatternBoost smoke run: sample one random target challenge, then run
# configs/pb_smoke_remote.yaml against it. Everything lands in output/.
set -euo pipefail
python -m experiments.pattern_boost.experiment_local_search sample configs/sample_small.yaml
target=$(ls output/sample/relators_sample_n*.json | grep -v -e factorwords -e config | head -1)
sed "s#__CHALLENGES__#${target}#" configs/pb_smoke_remote.yaml > output/pb_smoke_resolved.yaml
python -m experiments.pattern_boost.pattern_boost_main run output/pb_smoke_resolved.yaml
