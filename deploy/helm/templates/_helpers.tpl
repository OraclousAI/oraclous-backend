{{/*
Common name helpers for the oraclous-backend chart.
*/}}
{{- define "oraclous-backend.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "oraclous-backend.labels" -}}
app.kubernetes.io/name: {{ include "oraclous-backend.name" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
