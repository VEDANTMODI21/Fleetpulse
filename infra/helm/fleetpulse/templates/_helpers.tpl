{{- define "fp.labels" -}}
app.kubernetes.io/name: fleetpulse
app.kubernetes.io/version: {{ .Chart.AppVersion }}
{{- end -}}
{{- define "fp.pod" -}}
securityContext: {runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: RuntimeDefault}}
containers:
  - name: {{ .name }}
    image: "{{ .root.Values.image.repository }}:{{ .root.Values.image.tag }}"
    imagePullPolicy: {{ .root.Values.image.pullPolicy }}
    {{- with .command }}
    command: {{ toJson . }}
    {{- end }}
    envFrom:
      - configMapRef: {name: fleetpulse-config}
      - secretRef: {name: {{ .root.Values.secrets.existingSecret }}}
    securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
    resources: {{ toJson .resources }}
    {{- if .port }}
    ports: [{containerPort: {{ .port }}}]
    readinessProbe: {httpGet: {path: /healthz, port: {{ .port }}}, periodSeconds: 5}
    livenessProbe: {httpGet: {path: /healthz, port: {{ .port }}}, periodSeconds: 10}
    {{- end }}
    volumeMounts: [{name: tmp, mountPath: /tmp}]
volumes: [{name: tmp, emptyDir: {}}]
terminationGracePeriodSeconds: 30
topologySpreadConstraints:
  - {maxSkew: 1, topologyKey: topology.kubernetes.io/zone, whenUnsatisfiable: ScheduleAnyway,
     labelSelector: {matchLabels: {app: {{ .name }}}}}
{{- end -}}
