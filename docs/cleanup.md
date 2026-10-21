# Cleanup

Commands: [README "Clean up"](../README.md#clean-up). Not legal advice.

## What `make destroy` does (`scripts/cleanup.sh`)
1. Asks for the stack name (`CONFIRM_STACK`), saves the asset manifest, runs `cdk destroy`.
2. Deletes log groups created by service handlers (`/aws/lambda/<stack>…`, `/aws/lambda/<prefix>-…`).
3. Force-deletes secrets named `<prefix>/…` that are in their recovery window.
4. Lists data left behind (buckets with all versions, tables, `/<prefix>/` log groups) and this
   project's objects in the CDK bootstrap assets bucket, then asks again (`CONFIRM_DATA=yes`)
   before deleting them. Bootstrap objects are matched by the asset hashes in `cdk.out` and
   `.deploy/*.assets.json`; CDK's shared provider packages and the CDKToolkit stack are not touched.
5. A failing step prints a warning and the script continues.

## What `make verify-cleanup` checks (`scripts/verify_cleanup.py`, read-only)
Per region: stack, buckets (with version and delete-marker counts), DynamoDB tables, log groups,
Lambda functions and layers, state machines, HTTP APIs, guardrails, secrets (including scheduled
deletion), KMS aliases, tagged resources (each confirmed with the owning service, because the
tagging index lags), bootstrap asset objects. Once: IAM roles and policies, CloudFront
distributions, origin access controls, response headers policies. Every list call is paginated.
KMS keys in `PendingDeletion` are printed with their deletion date and are not counted as residue.
Exit code 1 if anything is left; the conclusion is either "Cleaned up: nothing … remains" or
"Residual resources remain: N".

Check every region you deployed to: `make verify-cleanup` with `--regions us-east-1 us-west-2`
(through `scripts/verify_cleanup.sh`).
