# Prior art

Searched on 2026-10-21: GitHub (aws-samples, aws-solutions-library-samples, awslabs and general
search for "photo booth", "photobooth", "photo kiosk", "selfie", "avatar", "presigned url",
"privacy", "data residency" with Bedrock/Stable Diffusion), AWS documentation examples and AWS
Solutions Library guidance. Not legal advice.

| Asset | Link | Covers | Does not cover |
|---|---|---|---|
| ComfyUI on AWS with Avatar App and Gallery | https://github.com/aws-samples/comfyui-personalized-generative-ai-avatars-app | self-hosted Stable Diffusion (ComfyUI on ECS/EC2), avatar app and gallery | consent flow, temporary storage with verified deletion, log hygiene, region pinning, review templates; the gallery keeps images |
| Image generator with Stable Diffusion on Bedrock (Streamlit) | https://github.com/aws-samples/image-generator-with-stable-diffusion-on-amazon-bedrock-using-streamlit | text-to-image web app on ECS calling Bedrock | consent, deletion, image input handling, audit |
| Image generation with Bedrock (.NET) | https://github.com/aws-samples/image-generation-using-amazon-bedrock-api-with-dotnet | calling Bedrock image models from .NET | everything beyond the model call |
| Guidance for asynchronous image generation with Stable Diffusion | https://github.com/aws-solutions-library-samples/guidance-for-asynchronous-inference-with-stable-diffusion-on-aws | scalable async SD inference on EKS with SQS/KEDA/Karpenter | privacy controls, deletion, consent; infrastructure is GPU/EKS-centric |
| Securing S3 presigned URLs for serverless applications | https://github.com/aws-samples/sample-securing-amazon-s3-presigned-urls-for-serverless-applications | presigned URL hardening (checksums, dynamic expiry, UUID keys) | the kiosk flow, version-aware deletion, consent, logs |
| Presigned URLs with temporary conditional credentials | https://github.com/aws-samples/presigned-url-using-temporary-conditional-credentials-for-amazon-s3 | restricting presigned URL use with conditional credentials | the rest of the flow |
| Privacy-preserving document AI with Bedrock | https://github.com/aws-samples/sample-privacy-preserving-document-ai-bedrock | PII masking before LLM analysis for documents, Guardrails as a check | images, deletion proof, consent; different data type |
| Data-residency chatbot with AgentCore | https://github.com/aws-samples/sample-data-residency-chatbot-with-bedrock-agentcore | single-region residency for a text-to-SQL chatbot, in-region inference | images, deletion, consent |
| Multi-region data residency | https://github.com/aws-samples/multi-region-data-residency | per-region deployments with routing for PII/PHI | image processing, deletion, consent, Bedrock |
| AI experience platform (community case studies) | https://github.com/hak2881/ai-experience-platform | write-ups of a commercial photo kiosk: job queue, presigned media without credentials, kiosk client | no source code; no consent/deletion/audit design or tests; not region-pinned |

## Differences of this project

None of the assets above combines these, and none tests them:

1. **Privacy defaults as the first constraint**: consent bound to the exact notice text, decline
   creates nothing, images exist only for the session.
2. **Verifiable deletion**: version-aware purge (every version and delete marker) with a residual
   listing, signed deletion receipts, automated tests including a negative control, and a cloud
   check that nothing is left; lifecycle/TTL only as backstop.
3. **Logs without personal data**: allowlisted audit schema, access log without IP/path, data
   protection policy, tests and a cloud canary.
4. **Region pinning by parameter**: the same stack synthesised for two regions in CI, a region check
   on the template, cross-region inference only with an explicit opt-in.
5. **Compliance review templates** (data flow, inventory, retention/deletion, third parties and
   cross-border) tied to the code.

Conclusion: no mature public implementation of this combination was found; development continued.
