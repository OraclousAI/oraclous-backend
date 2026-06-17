{{/*
Common helpers for the oraclous-backend chart.

The chart is DRY: a single generic Deployment+Service template ranges over
`.Values.services`, and a single generic Job template ranges over `.Values.migrations`.
These helpers centralise naming, labels, the image reference, and — critically — the
two security primitives this chart encodes:

  1. The RLS DSN split. Every RUNTIME workload connects to Postgres as the NOSUPERUSER /
     NOBYPASSRLS `oraclous_app` role; every migrate/seed Job connects as the OWNER
     (`oraclous`) role. A runtime pod can never obtain the owner DSN and a migrate Job can
     never obtain the oraclous_app DSN, because the DSN env is assembled by
     `oraclous-backend.pgDsnEnv` purely from a workload's single `dsnRole` field ("app" for
     runtime, "owner" for migrate/seed) — there is no place to pass a literal DSN.

  2. Secrets as secretKeyRef ONLY. No sensitive value (DB passwords, JWT_SECRET,
     INTERNAL_SERVICE_KEY, OAUTH_ENC_KEY/ENCRYPTION_KEY, Neo4j role passwords, the LLM key,
     OAuth client secrets, …) is ever rendered as a literal into a manifest. The DB password
     is delivered as its OWN env var from a Secret, and the DSN is composed with Kubernetes
     dependent-env-var expansion ($(VAR)) so the secret only ever lives in the Secret object.
*/}}

{{- define "oraclous-backend.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "oraclous-backend.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "oraclous-backend.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Chart-wide labels applied to every object. */}}
{{- define "oraclous-backend.labels" -}}
helm.sh/chart: {{ include "oraclous-backend.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: oraclous-backend
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{/* Per-workload selector labels. Call with a dict: {root, name}. */}}
{{- define "oraclous-backend.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end -}}

{{/*
Image reference for a workload. Call with a dict: {root, image}.
A non-empty `.Values.global.imageRegistry` is prepended; `tag` defaults to the chart appVersion.
*/}}
{{- define "oraclous-backend.image" -}}
{{- $registry := .root.Values.global.imageRegistry -}}
{{- $repo := .image.repository -}}
{{- $tag := default .root.Chart.AppVersion .image.tag -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry $repo $tag -}}
{{- else -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
{{- end -}}

{{/*
Emit the Postgres DSN env vars for a workload (the RLS-split core).

Call with a dict: {root, ctx} where ctx is one service/migration entry that has a `postgres`
block. ctx.postgres = { enabled, role ("app"|"owner"), urlEnv, extra: [ {env, role} ] }.

For the primary DSN it emits a pair of env vars:

    <ROLE>_PG_PASSWORD : valueFrom.secretKeyRef -> the app OR owner password Secret
    <urlEnv>           : "postgresql+asyncpg://<user>:$(<ROLE>_PG_PASSWORD)@<host>:<port>/<db>"

Kubernetes expands `$(<ROLE>_PG_PASSWORD)` from the sibling env var at pod start, so the
password value exists only inside the Secret object — never in this manifest. The role->user
and role->Secret mapping is fixed here, so a workload that declares role "app" structurally
cannot receive the owner credential (and vice-versa).

`extra` lets a workload declare a SECOND DSN with a different role — used by:
  * the gateway (OWNER_DATABASE_URL, role "owner", for its 2 pre-auth producer reads), and
  * the execution-engine (ENGINE_MAINTENANCE_DATABASE_URL, role "owner", cross-org sweeps).
*/}}
{{- define "oraclous-backend.pgDsnEnv" -}}
{{- $root := .root -}}
{{- $pg := $root.Values.substrate.postgres -}}
{{- $ctx := .ctx -}}
{{- $pgc := $ctx.postgres -}}
{{- $primaryRole := default "app" $pgc.role -}}
{{- $entries := list (dict "env" (default "DATABASE_URL" $pgc.urlEnv) "role" $primaryRole) -}}
{{- range $e := (default (list) $pgc.extra) -}}
{{- $entries = append $entries $e -}}
{{- end -}}
{{- /* Deduplicate the password env vars so we declare APP_PG_PASSWORD / OWNER_PG_PASSWORD at most once each. */ -}}
{{- $needApp := false -}}
{{- $needOwner := false -}}
{{- range $e := $entries -}}
{{- if eq (default "app" $e.role) "owner" -}}{{- $needOwner = true -}}{{- else -}}{{- $needApp = true -}}{{- end -}}
{{- end -}}
{{- if $needApp }}
- name: APP_PG_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ required "substrate.postgres.appPassword.secretName is required (oraclous_app DB password)" $pg.appPassword.secretName | quote }}
      key: {{ $pg.appPassword.secretKey | quote }}
{{- end }}
{{- if $needOwner }}
- name: OWNER_PG_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ required "substrate.postgres.ownerPassword.secretName is required (owner DB password)" $pg.ownerPassword.secretName | quote }}
      key: {{ $pg.ownerPassword.secretKey | quote }}
{{- end }}
{{- range $e := $entries }}
{{- $role := default "app" $e.role }}
{{- $user := ternary $pg.ownerUser $pg.appUser (eq $role "owner") }}
{{- $pwRef := ternary "$(OWNER_PG_PASSWORD)" "$(APP_PG_PASSWORD)" (eq $role "owner") }}
- name: {{ $e.env | quote }}
  value: "{{ $pg.driver }}://{{ $user }}:{{ $pwRef }}@{{ required "substrate.postgres.host is required" $pg.host }}:{{ $pg.port }}/{{ $pg.database }}{{ $pg.query }}"
{{- end }}
{{- end -}}

{{/*
Emit a list of env vars from a workload's `env.fromValues` map (non-secret literals).
Call with a dict: {map}.
*/}}
{{- define "oraclous-backend.envFromValues" -}}
{{- range $k, $v := .map }}
- name: {{ $k | quote }}
  value: {{ $v | quote }}
{{- end -}}
{{- end -}}

{{/*
Emit inter-service URL env vars from a workload's `env.urls` map (envName -> target service key).
The hostname is computed as <Release.Name>-<serviceKey>, so service discovery never hardcodes the
release name in values. Port is the target service's service.port (default 8000).
Call with a dict: {root, map}.
*/}}
{{- define "oraclous-backend.envUrls" -}}
{{- $root := .root -}}
{{- range $env, $target := .map }}
{{- $tsvc := index $root.Values.services $target }}
{{- if not $tsvc }}{{- fail (printf "env.urls target %q (for %s) is not a service in .Values.services" $target $env) }}{{- end }}
{{- $tport := default 8000 $tsvc.service.port }}
- name: {{ $env | quote }}
  value: "http://{{ printf "%s-%s" $root.Release.Name $target | trunc 63 | trimSuffix "-" }}:{{ $tport }}"
{{- end -}}
{{- end -}}

{{/*
Emit Redis URL env vars from a workload's `env.redis` map (envName -> db index). The base URL is
substrate.redis.url; the db index is appended as /<n>. If the operator's URL already carries a
path, they should leave it bare (scheme://host:port) so the db suffix is appended cleanly.
Call with a dict: {root, map}.
*/}}
{{- define "oraclous-backend.envRedis" -}}
{{- $root := .root -}}
{{- $base := required "substrate.redis.url is required (a service declares a Redis env)" $root.Values.substrate.redis.url -}}
{{- $base = trimSuffix "/" $base -}}
{{- range $env, $db := .map }}
- name: {{ $env | quote }}
  value: "{{ $base }}/{{ $db }}"
{{- end -}}
{{- end -}}

{{/*
Emit Neo4j URI/user env vars for KGS or KRS from a workload's `env.neo4j` block:
  { role ("kgsWriter"|"krsReader"), uriEnv, userEnv }
URI + user come from .Values.neo4jRoles.<role>; the password is delivered separately via
env.fromSecrets (KGS_NEO4J_PASSWORD / KRS_NEO4J_PASSWORD) so it stays a secretKeyRef.
Call with a dict: {root, neo4j}.
*/}}
{{- define "oraclous-backend.envNeo4j" -}}
{{- $root := .root -}}
{{- $n := .neo4j -}}
{{- $r := index $root.Values.neo4jRoles $n.role -}}
{{- if not $r }}{{- fail (printf "env.neo4j.role %q is not under .Values.neo4jRoles" $n.role) }}{{- end }}
- name: {{ $n.uriEnv | quote }}
  value: {{ required (printf "neo4jRoles.%s.uri is required" $n.role) $r.uri | quote }}
- name: {{ $n.userEnv | quote }}
  value: {{ $r.user | quote }}
{{- end -}}

{{/*
Emit a list of env vars from a workload's `env.fromSecrets` map (secretKeyRef only).
Each entry: <ENV_NAME>: { secretName, secretKey }. secretName is REQUIRED (fail-closed: a
missing operator Secret stops the render rather than baking a literal).
Call with a dict: {map, workload}.
*/}}
{{- define "oraclous-backend.envFromSecrets" -}}
{{- $workload := .workload -}}
{{- range $env, $ref := .map }}
- name: {{ $env | quote }}
  valueFrom:
    secretKeyRef:
      name: {{ required (printf "a Secret name is required for env %s on workload %s (no secret literal allowed)" $env $workload) $ref.secretName | quote }}
      key: {{ default "value" $ref.secretKey | quote }}
{{- end -}}
{{- end -}}
