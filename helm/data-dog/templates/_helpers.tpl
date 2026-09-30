{{/* 通用命名与标签助手 */}}

{{- define "data-dog.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "data-dog.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "data-dog.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "data-dog.labels" -}}
helm.sh/chart: {{ include "data-dog.chart" . }}
{{ include "data-dog.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "data-dog.selectorLabels" -}}
app.kubernetes.io/name: {{ include "data-dog.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* 镜像引用：tag 空则回退到 appVersion */}}
{{- define "data-dog.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}

{{- define "data-dog.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "data-dog.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* 共享的 envFrom 来源：ConfigMap（非敏感）+ Secret（敏感，两种来源二选一）*/}}
{{- define "data-dog.envFrom" -}}
- configMapRef:
    name: {{ include "data-dog.fullname" . }}-config
{{- if .Values.secret.existingSecret }}
- secretRef:
    name: {{ .Values.secret.existingSecret }}
{{- else if .Values.secret.create }}
- secretRef:
    name: {{ include "data-dog.fullname" . }}-secret
{{- end }}
{{- end -}}

{{/* 被两个 deployment 共用的调度相关片段由各自模板内联（values 不同）*/}}
