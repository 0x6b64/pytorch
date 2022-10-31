#!/bin/bash
set -x
export https_proxy=http://fwdproxy:8080 http_proxy=http://fwdproxy:8080 no_proxy=.fbcdn.net,.facebook.com,.thefacebook.com,.tfbnw.net,.fb.com,.fburl.com,.facebook.net,.sb.fbsbx.com,localhost
export TORCHDYNAMO_DYNAMIC_SHAPES=1
export AOT_DYNAMIC_SHAPES=1
DATE="$(date)"
#FLAG="--backend inductor"
FLAG="--accuracy --backend inductor --training"
#FLAG="--accuracy --backend inductor"
# shellcheck disable=SC2086 # Intended splitting of FLAG
python benchmarks/dynamo/torchbench.py --output torchbench.csv --accuracy $FLAG 2>&1 | tee torchbench.log
# shellcheck disable=SC2086 # Intended splitting of FLAG
python benchmarks/dynamo/huggingface.py --output huggingface.csv --accuracy $FLAG 2>&1 | tee huggingface.log
# shellcheck disable=SC2086 # Intended splitting of FLAG
python benchmarks/dynamo/timm_models.py  --output timm_models.csv --accuracy $FLAG 2>&1 | tee timm_models.log
cat torchbench.log huggingface.log timm_models.log | gh gist create -d "Sweep logs for $(git rev-parse --abbrev-ref HEAD) $FLAG (TORCHDYNAMO_DYNAMIC_SHAPES=$TORCHDYNAMO_DYNAMIC_SHAPES) - $DATE" -
cat torchbench.csv huggingface.csv timm_models.csv | gh gist create -d "Sweep csv for $(git rev-parse --abbrev-ref HEAD) $FLAG (TORCHDYNAMO_DYNAMIC_SHAPES=$TORCHDYNAMO_DYNAMIC_SHAPES) - $DATE" -

