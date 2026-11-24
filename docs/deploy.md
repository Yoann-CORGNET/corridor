# Deployment

The backend runs as one container on Google Cloud Run, deployed by the CI on every push to
`main` that touches `backend/` or `.github/workflows/ci.yml`. It runs the scenario
`hongkong_relay.json`. The front end is a static build on Vercel that points at it.

## Backend image

Built from `backend/` (`backend/Dockerfile`):

```sh
docker build -t corridor-backend backend
docker run --rm -p 8080:8080 corridor-backend          # ws://127.0.0.1:8080/ws
docker run --rm -p 8080:8080 -e CORRIDOR_SCENARIO=annecy.json corridor-backend
```

| Variable            | Default     | Meaning                                        |
| ------------------- | ----------- | ---------------------------------------------- |
| `PORT`              | `8080`      | Listening port (Cloud Run sets it).            |
| `CORRIDOR_SCENARIO` | `demo.json` | File of `backend/scenarios/` to load at start. |

## Cloud Run settings

The backend is stateful: it keeps the world state in memory, runs the simulation clock in a
background task and serves one WebSocket per client. Cloud Run's defaults break all three, so
the service needs these settings:

| Flag                   | Why                                                             |
| ---------------------- | --------------------------------------------------------------- |
| `--min-instances 1`    | The state lives in memory; scaling to zero resets the scenario. |
| `--max-instances 1`    | A second instance would hold a different world.                 |
| `--no-cpu-throttling`  | The clock ticks between requests; throttled CPU freezes it.     |
| `--timeout 3600`       | Maximum WebSocket lifetime (60 min); the front end reconnects.  |
| `--session-affinity`   | Keeps a client on the same instance across reconnects.          |
| `--cpu 2 --memory 2Gi` | The planner runs in its own worker process next to the server.  |

These flags live in the `deploy-backend` job of `.github/workflows/ci.yml` and are applied on
every deploy: change them there, not in the console (the next deploy would overwrite the
console). Same for the scenario (`CORRIDOR_SCENARIO`, set to `hongkong_relay.json`): changing
it in `ci.yml` and merging redeploys. Every deploy restarts the world.

## Continuous deployment

On a push to `main`, once the lint and test jobs pass, `deploy-backend`:

1. skips everything if neither `backend/` nor `ci.yml` changed;
2. builds the image and pushes it to Docker Hub, public, tagged with the commit SHA
   (`docker.io/<user>/corridor-backend:<sha>`);
3. authenticates to GCP with Workload Identity Federation (GitHub's OIDC token exchanged for
   short-lived credentials: no GCP key is stored anywhere);
4. deploys that exact image to the `corridor-backend` service.

Cloud Run does not watch the registry: a push to Docker Hub alone changes nothing, step 4 is
what rolls out the new image. Cloud Run pulls the image once per revision.

Rolling back means deploying an older SHA:
`gcloud run deploy corridor-backend --region europe-west9 --image docker.io/<user>/corridor-backend:<old-sha>`,
or routing traffic to a previous revision in the console.

### One-time Docker Hub setup

1. Create the public repository `corridor-backend` on Docker Hub.
2. Create an access token (Account settings > Personal access tokens) with *Read & Write*
   scope, and store it in GitHub without printing it:

```sh
gh secret set DOCKERHUB_TOKEN          # prompts for the value
gh variable set DOCKERHUB_USERNAME --body <docker-hub-user>
```

### One-time GCP setup

Run once, by someone with Owner on the GCP project. Two service accounts: `corridor-deploy`
(used by GitHub Actions to deploy) and `corridor-run` (identity of the running service, no
role: the backend calls no GCP API).

```sh
PROJECT_ID=<gcp-project-id>
REGION=europe-west9
REPO=Yoann-CORGNET/corridor
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
DEPLOY_SA=corridor-deploy@$PROJECT_ID.iam.gserviceaccount.com
RUNTIME_SA=corridor-run@$PROJECT_ID.iam.gserviceaccount.com
gcloud config set project "$PROJECT_ID"

gcloud services enable run.googleapis.com iamcredentials.googleapis.com sts.googleapis.com

gcloud iam service-accounts create corridor-run
gcloud iam service-accounts create corridor-deploy

# Deploy account: deploy revisions, run them as corridor-run.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$DEPLOY_SA" --role=roles/run.developer
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:$DEPLOY_SA" --role=roles/iam.serviceAccountUser

# Workload Identity Federation: only pushes to main of this repository get credentials.
gcloud iam workload-identity-pools create github --location=global
gcloud iam workload-identity-pools providers create-oidc corridor \
  --location=global --workload-identity-pool=github \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping=google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref \
  --attribute-condition="assertion.repository == '$REPO' && assertion.ref == 'refs/heads/main'"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"

# GitHub repository variables read by the workflow (identifiers, not secrets).
gh variable set GCP_PROJECT_ID --body "$PROJECT_ID"
gh variable set GCP_WIF_PROVIDER \
  --body "projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/corridor"
gh variable set GCP_DEPLOY_SA --body "$DEPLOY_SA"
gh variable set GCP_RUNTIME_SA --body "$RUNTIME_SA"
```

After the first deploy has created the service, make it public (the deploy account cannot
change IAM policies, on purpose):

```sh
gcloud run services add-iam-policy-binding corridor-backend --region="$REGION" \
  --member=allUsers --role=roles/run.invoker
```

If the organisation enforces domain-restricted sharing, this binding is refused: the policy
must allow `allUsers` on this project.

### Manual deploy (fallback)

Without the CI, from the repository root, with the flags of the table above:

```sh
gcloud run deploy corridor-backend \
  --source backend \
  --region europe-west9 \
  --service-account corridor-run@<gcp-project-id>.iam.gserviceaccount.com \
  --min-instances 1 --max-instances 1 \
  --no-cpu-throttling \
  --timeout 3600 \
  --session-affinity \
  --cpu 2 --memory 2Gi \
  --set-env-vars CORRIDOR_SCENARIO=hongkong_relay.json
```

`--source` builds the Dockerfile with Cloud Build. The next CI deploy replaces this revision.

## Front end

Vercel's Git integration builds and deploys the front end on every push; no workflow needed.
In the Vercel project settings:

- Root directory: `frontend`.
- Environment variable `VITE_WS_URL=wss://<service-url>/ws` (Vite reads it at build time).

The HTTP calls (`/api/...`) are relative to the page, so Vercel must forward them to the
backend. A rewrite in `frontend/vercel.json` does it (WebSockets cannot go through a rewrite,
hence `VITE_WS_URL`):

```json
{
  "rewrites": [{ "source": "/api/:path*", "destination": "https://<service-url>/api/:path*" }]
}
```
