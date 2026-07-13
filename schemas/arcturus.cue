// arcturus.cue - CUE schema for Arcturus stack manifests
package arcturus

// The top-level value must be a Stack
#Stack: {
	apiVersion: "arcturus.u128.org/v1"
	kind:       "Stack"

	metadata: {
		name:      string
		namespace: string | *"default"
		labels?: [string]: string
		annotations?: [string]: string
	}

	spec: {
		services: [string]: #Service
		redirects?: [string]: #Redirect
		network?: {
			isolate: bool | *true
			external?: [...string]
		}
		deploy?: {
			managed:    bool | *false
			strategy:   "docker-compose" | "quadlet" | *"docker-compose"
			autoUpdate: bool | *false
			healthCheck?: string
		}
		security?: {
			corsOrigins?: [...string]
			rateLimit?: string
		}
	}
}

#Service: {
	port:     int & >0 & <65536
	protocol: "http" | "https" | "tcp" | "udp" | *"http"
	domains?: [...string]
	aliases?: [...string]
	type:     "proxy" | "static" | "tcp-forward" | "udp-forward" | *"proxy"
	websocket?:    bool | *false
	maxBodySize?:  string | *"1G"
	nginxExtras?:  string
	healthCheck?:  string
	containerName?: string
}

#Redirect: {
	from:  string
	to:    string
	code?: int & >=300 & <400 | *301
}

#ServiceRelease: {
	apiVersion: "arcturus.u128.org/v2"
	kind:       "ServiceRelease"
	metadata: {
		name:     =~"^[a-z0-9][a-z0-9-]{0,62}$"
		revision: =~"^[0-9a-f]{40}$"
	}
	spec: {
		components: [string]: {
			image: =~"^[a-z0-9][a-z0-9._-]*(?::[0-9]+)?(?:/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$"
			containerName?: =~"^[a-z0-9][a-z0-9-]{0,62}$"
			mode?: "service" | "oneshot" | "scheduled" | *"service"
			command?: [...string]
			environment?: [=~"^[A-Za-z_][A-Za-z0-9_]*$"]: string
			secrets?: [...{
				name: string
				target?: string
				type?: "file" | "env" | *"file"
			}]
			ports?: [...{
				container: int & >0 & <65536
				host?: int & >0 & <65536
				hostIp?: string
				protocol?: "tcp" | "udp" | *"tcp"
			}]
			volumes?: [...{
				source: string
				target: =~"^/"
				type?: "bind" | "volume" | *"bind"
				readOnly?: bool | *false
				external?: bool | *true
				selinuxRelabel?: "private" | "shared"
			}]
			dependsOn?: [...string]
			networks?: [...string]
			healthCheck?: {
				command: string
				interval?: string | *"10s"
				timeout?: string | *"5s"
				retries?: int & >=1 & <=30 | *5
				startPeriod?: string | *"10s"
			}
			schedule?: {
				onCalendar: string
				persistent?: bool | *true
				randomizedDelaySeconds?: int & >=0 & <=86400 | *0
				runOnDeploy?: bool | *false
			}
			restart?: "always" | "on-failure" | "no" | *"always"
		}
		networks?: [...{name: string, external?: bool | *true}]
		routing?: [string]: {
			component: string
			port: int & >0 & <65536
			protocol?: "http" | "https" | "tcp" | "udp" | *"http"
			domains?: [...string]
			aliases?: [...string]
			websocket?: bool | *false
			maxBodySize?: string | *"1G"
		}
		deployment?: {
			timeoutSeconds?: int & >=10 & <=1800 | *300
			rollbackOnFailure?: bool | *true
		}
	}
}

// Repository manifests may use the legacy routing schema or a complete v2 release.
#Manifest: #Stack | #ServiceRelease
#Manifest
