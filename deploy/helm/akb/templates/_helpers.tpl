{{- define "akb.labels" -}}
app.kubernetes.io/name: akb
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: akb
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "akb.selectorLabels" -}}
app.kubernetes.io/name: akb
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "akb.publicHost" -}}
{{- .Values.global.publicUrl | trimPrefix "https://" | trimPrefix "http://" | trimSuffix "/" -}}
{{- end }}

{{- define "akb.keycloakHost" -}}
{{- .Values.sso.keycloakPublicUrl | trimPrefix "https://" | trimPrefix "http://" | trimSuffix "/" -}}
{{- end }}
