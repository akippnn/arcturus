// arcturus.cue - CUE schema for Arcturus stack manifests
package arcturus

import "strings"

// The top-level value must be a Stack
#Stack: {
	apiVersion: "arcturus.u128.org/v1"
	kind:       "Stack"

	metadata: {
		name:      =~"^[a-z0-9][a-z0-9_-]{0,62}$"
		namespace: =~"^[a-z0-9][a-z0-9_-]{0,62}$" | *"default"
		labels?: [=~"^.{1,128}$"]: string & strings.MaxRunes(1024)
		annotations?: [=~"^.{1,128}$"]: string & strings.MaxRunes(2048)
	}

	spec: {
		services: [=~"^[a-z0-9][a-z0-9_-]{0,62}$"]: #Service
		redirects?: [=~"^[a-z0-9][a-z0-9_-]{0,62}$"]: #Redirect
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
	domains?: [...=~"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$"]
	aliases?: [...=~"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"]
	type:     "proxy" | "static" | "tcp-forward" | "udp-forward" | *"proxy"
	websocket?:    bool | *false
	maxBodySize?:  =~"^[1-9][0-9]{0,8}[kKmMgG]?$" | *"1G"
	nginxExtras?:  =~"^[^{}\r\n\x00]*$" & strings.MaxRunes(4096)
	healthCheck?:  string & strings.MaxRunes(2048)
	containerName?: =~"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
}

#Redirect: {
	from:  =~"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$"
	to:    =~"^https?://"
	code?: 301 | 302 | 307 | 308 | *301
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
