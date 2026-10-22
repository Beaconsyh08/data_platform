# Architecture upgrade completion review

This review covers the operator/pipeline implementation based on `ce6e2680`, the source-cleanup baseline
`dca187a1`, and the subsequent legacy console separation. It distinguishes delivered contracts from
hardware-dependent acceptance. Installed release identity and online evidence belong to the immutable
release directory and must be checked independently.

| Original requirement | Implementation and review evidence | Status |
| --- | --- | --- |
| Unified versioned operators, parameters, resources and lazy availability | `operators/`; `test_operators` checks discovery without model imports, pinned versions and generic dispatch | Implemented |
| Map all existing processing entry families; keep privileged mutations separate | Migration inventory in `data_platform_operators.md`; registered background-entry coverage includes compatibility submission services | Implemented with explicit compatibility adapters |
| Shared submission, stable execution snapshots, existing Job/Attempt admission | `processing/commands.py`, `console_submissions.py`, `service.py`, local executor and Agent; local execution and preprocessing route suites | Implemented |
| Sequential versioned pipelines, upstream dataset/evidence references, APIs | Planner/service/routes; immutable revision and input identity tests | Implemented |
| Durable coordination, review, cancellation, retry and publication acknowledgement | Coordinator/systemd plus concurrent tick, maintenance, restart, stale decision and retained-output tests | Implemented; representative local/remote flows accepted |
| Selective laundry absorption | Optional EEF spike strategy, C3 reuse, pose interpolation bound to reviewed recipe; v2/v3 and shared-shard preservation tests | Implemented; representative remote v3 repair accepted |
| Source modification permissions and compatibility | Original endpoint names and global guards retained; source operations excluded from automatic chains | Implemented |
| Extract registration, caches, submission and result association from old entry point | `services/`, `processing/console_submissions.py`, thin compatibility route modules; one shared runtime per server | Completed in console separation |
| Split browsing, operator configuration, workflow editing and run state | `templates/console/`, shared mounted pipeline editor, preserved form bindings; composed-script and browser tests | Completed in console separation |
| Discard stale responses on account/environment/dataset changes | Scoped read generations; late response/error tests; embedded editor disposal on context switch | Implemented |
| Confirmed missing-source retirement and owned-cache reclamation | Existing source cleanup protocol, leases and ownership proofs retained; catalog removal clears shared runtime caches | Implemented; local/remote acceptance required for each release |
| AI capability description, preview and execution interfaces | Operator and pipeline APIs | Implemented |
| MCP, conversational UI and autonomous planning | Explicitly excluded from this round by the original plan | Deferred by design |
| Every model/media/conversion/build operator exercised on its real execution runtime | Contract and compatibility suites exist; some model backends/Embedding and local MCAP runtime were unavailable at the `.67` baseline | Full real-runtime acceptance remains outstanding |
| Offline FLORA caption/reward pipeline | Existing caption Job/Attempt and publication contracts; isolated GPU environments; per-method capabilities and raw-window disagreement review | Implemented; native A100 and local browser acceptance, online rollout separate |
| Production promotion | Same immutable package requires development evidence and the existing production approval mechanism | Separate release step |

## Scope of the console completion

Compatibility adapters are intentional: preserving external endpoints does not require keeping registration,
cache management or worker submission inside `viewer.run_server`. Those responsibilities now delegate to
services. Episode rendering and privileged legacy source-edit implementations remain feature-specific and
retain their existing protections; the plan does not require replacing their algorithms.

Verification covers the original API/CLI tests, shared registry and result callbacks outside a Flask request,
independent console instances, composed JavaScript state, asynchronous response isolation, real-browser
form/navigation behavior, the embedded and standalone editors, mobile width and Viewer role restrictions.
The release validation set includes these regression modules. Optional browser tests additionally require
an installed Chrome and Playwright; their explicit run must be recorded with the release acceptance evidence.

The remaining model-runtime acceptance is not a missing console split. Passing CPU fixtures or advertising
an unavailable operator does not establish real model inference acceptance, and development installation
does not constitute production promotion.

Installed acceptance also exercises the original local cache-preparation endpoint. Local command snapshots
retain requested parameters, while execution binds outputs to supervisor staging and uses its worker limits.
Viewer artifacts enter the normal upload/acknowledgement protocol, and custom dataset output paths are
included in admission and path locks. Real-process regressions cover cache publication and local v2.1/v3.0
dataset transforms, checking that the final destination is absent until publication and source bytes remain
unchanged. A completed attempt awaiting upload is finalized from its retained output rather than recomputed.
