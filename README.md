# Lingee Bench

**English** | [简体中文](README.zh-CN.md)

Lingee Bench is an agent evaluation framework built on [Harbor](https://github.com/harbor-framework/harbor). It provides harness adapters, model and MCP proxies, an offline ERP mock service, and batch-run and scoring tools. The tasks, input data and graders are published as a dataset on Hugging Face: [KD-Lingee/lingee-bench](https://huggingface.co/datasets/KD-Lingee/lingee-bench).

| Split | Tasks | Where to get it |
| --- | ---: | --- |
| Enterprise | 30 | Download from Hugging Face into `datasets/lg-bench-enterprise-v1.0/`; finance, manufacturing, supply chain |
| General | 35 | Download from Hugging Face into `datasets/lg-bench-general-v1.0/`; data analysis, documents, retrieval, images and scheduling |
| Examples | 2 | Shipped with the repository in [examples/tasks](examples/tasks); for framework self-tests and task authoring, not part of official scores |

Enterprise and general results are reported separately. A release is identified by the Git commit or tag of this repository, the Hugging Face revision of the dataset, the task manifest and the file digests together.

Supported harnesses: Claude Code, Pi, DeepSeek Harness, OpenCode. See the [docs index](docs/README.md) for runtime requirements and configuration.

## Let a coding agent walk you through it

The repository ships skills under `.agents/skills/`. A coding agent that supports skills can follow them from a fresh clone to final scores. It shows each command before running it and asks before anything that costs money:

- [lg-bench-setup](.agents/skills/lg-bench-setup/SKILL.md): install, dataset download, images, credentials, preflight and baselines, smoke test, general batch run, score summary.
- [erp-mock-batch-eval](.agents/skills/erp-mock-batch-eval/SKILL.md): the 30-task ERP batch run and its summary.

Open the agent in the repository root and state your goal, for example:

> Use lg-bench-setup to get OpenCode + glm-5.3-flash running: first the example smoke test, then all 35 general tasks, and summarize the scores.

Put your keys into `.env.glm` yourself; do not paste them into the chat. The manual steps below follow the same flow.

## 1. Install

Requires Python 3.12, [uv](https://docs.astral.sh/uv/) and a working Docker (API ≥ 1.48, macOS or Linux).

```bash
uv sync --locked
cp .env.example .env.glm
```

Fill in the model endpoint and key in `.env.glm`: `LG_OPENAI_*` for harnesses that speak the OpenAI-compatible protocol, `LG_ANTHROPIC_*` for Claude Code. The judge used by the general and ERP tasks speaks the OpenAI-compatible protocol, so `LG_OPENAI_*` is required for those tasks. Credential files are never committed; real credentials only reach the model proxy.

Build the model proxy and the harness image you need, OpenCode as an example:

```bash
docker build -f docker/model-proxy/Dockerfile -t lg-bench/model-proxy:0.1.0 .
uv run lg-bench harness build opencode
```

## 2. Smoke test on the example task (no dataset needed)

Check that the pipeline and credentials work:

```bash
uv run lg-bench preflight --config configs/jobs/opencode-glm-5.2-smoke.yaml --env-file .env.glm --require-env
uv run harbor run --config configs/jobs/opencode-glm-5.2-smoke.yaml --env-file .env.glm --job-name smoke-opencode
uv run python .agents/skills/lg-bench-setup/scripts/summarize_job.py jobs/smoke-opencode --env-file .env.glm
```

This job uses `glm-5.2`, which is the only model the proxy allows by default. To use another model, set `LG_PROXY_ALLOWED_MODELS` in `.env.glm` and change `model_name` in a `*.local.yaml` copy of the job.

## 3. Download the datasets

See [datasets](datasets/README.md) for pinning a revision:

```bash
uvx --from huggingface_hub hf download KD-Lingee/lingee-bench \
  --repo-type dataset --revision <revision> --local-dir datasets \
  --include "lg-bench-*/**" --include "environments/**" --include "registry.json"
uv run python scripts/check_release.py --datasets
```

## 4. General tasks: batch run and scoring

Generate a batch job for any harness, agent model and judge model. Network isolation and judge wiring follow the official job. The script also writes a model allowlist fragment.

```bash
uv run python .agents/skills/lg-bench-setup/scripts/general_job.py \
  --harness-job configs/jobs/opencode-glm-5.2-smoke.yaml \
  --model glm-5.3-flash --judge-model glm-5.3-flash \
  --output configs/jobs/general-opencode-glm-5.3-flash.local.yaml
uv run lg-bench preflight --config configs/jobs/general-opencode-glm-5.3-flash.local.yaml --env-file .env.glm --require-env
uv run harbor run --config configs/jobs/general-opencode-glm-5.3-flash.local.yaml \
  --env-file .env.glm --job-name general-opencode-flash
uv run python .agents/skills/lg-bench-setup/scripts/summarize_job.py jobs/general-opencode-flash --env-file .env.glm
```

All 35 tasks run by default; use `--tasks` for a subset. Single-task runs and the network boundary are covered in [general task runs](docs/general-smoke.md).

## 5. ERP tasks: batch run and scoring

Build the MCP proxy and the offline ERP mock first. The mock image bakes in the downloaded evaluation instance from `datasets/environments/erp-mock-mcp/`, so rebuild it after updating the dataset:

```bash
docker build -f docker/mcp-proxy/Dockerfile -t lg-bench/mcp-proxy:0.1.0 .
docker build -f docker/erp-mock-mcp/Dockerfile -t lg-bench/erp-mock-mcp:0.1.0 .
```

Set `LG_PROXY_ALLOWED_MODELS` in `.env.glm` to include both the agent and the judge model, for example `glm-5.3-flash,glm-5.3`. Then generate the 30 per-task configs, run them one by one and summarize:

```bash
B=jobs/erp30-opencode
uv run python .agents/skills/erp-mock-batch-eval/scripts/batch.py prepare \
  --harness-job configs/jobs/opencode-glm-5.2-smoke.yaml \
  --model glm-5.3-flash --judge-model glm-5.3 --output $B
python3 -c "import json,sys; [print(t['task'], t['config'], t['job_dir']) for t in json.load(open(sys.argv[1]))['tasks']]" $B/batch.json |
while read -r task cfg dir; do
  [ -e "$dir" ] && continue   # skip tasks that already have results; safe to rerun after an interruption
  uv run harbor run --config "$cfg" --env-file .env.glm --job-name "$task" < /dev/null
done
uv run python .agents/skills/erp-mock-batch-eval/scripts/batch.py summary --batch $B/batch.json
```

Each task binds its own mock dataset and tool allowlist, so each task is its own job. Evaluation runs against the frozen mock published with the dataset, so no connection to a real ERP is needed. Scoring conventions are in [ERP scoring](docs/erp-scoring.md).

## Reading the scores

Summaries bucket trials by scoring status. Only officially graded trials enter the mean; execution errors, evaluation errors (including judge failures), missing scores and unfinished trials are listed separately and never counted as zero. When reporting, state the repository commit, dataset revision, harness and version, agent and judge models, number of attempts, and the denominator of the mean. Report the general and ERP splits separately.

## Checks and development

Without the datasets:

```bash
uv run lg-bench validate          # validates examples/tasks
uv run lg-bench smoke             # local reference solution and grader of the example task
uv run python scripts/check_release.py
uv run pytest -q                  # dataset-dependent tests are reported as skipped
```

After downloading the datasets:

```bash
uv run python scripts/check_release.py --datasets
uv run lg-bench validate --path datasets/lg-bench-enterprise-v1.0/tasks
uv run lg-bench validate --path datasets/lg-bench-general-v1.0/tasks
uv run pytest -q
```

## Layout

```text
examples/tasks/      example tasks (framework self-tests, authoring reference)
datasets/            download location for the datasets (not committed, see datasets/README.md)
erp-mock-mcp/        offline business service engine, demo instance and hand-modeled packs
configs/             Harbor job and compose configuration
src/lg_bench/        harness adapters, proxies, validation and preflight
scripts/             release checks and batch scripts
.agents/skills/      setup, batch-run and score-triage skills
docs/                usage and scoring documentation
```

## License

Our own code and documentation are licensed under [Apache-2.0](LICENSE). Third-party components keep their original authorship, copyright and license notices; see [NOTICE](NOTICE) for the scope. The dataset license is stated on the Hugging Face dataset card.
