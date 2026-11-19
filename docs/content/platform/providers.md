---
title: Supported Providers
weight: 40
description: The clouds and neoclouds Modelplane runs on today, and the Crossplane providers it grows into.
---
Modelplane is built on [Crossplane](https://crossplane.io) and shares its
infrastructure providers, so the set of clouds and neoclouds it reaches grows
alongside Crossplane itself. This page shows where Modelplane runs today and
where it's headed.

A provider can show up here in three ways:

{{< hint "note" >}}
- **Provisioning supported.** Modelplane creates and manages the whole cluster
  from an `InferenceCluster`, through the cloud's [cluster
  provider](#cluster-providers). GKE, EKS, AKS, Nebius mk8s, Vultr VKE, Civo
  K3s, CoreWeave CKS work this way today.
- **Bring your own supported.** Register a cluster you already run with
  `source: Existing`. This works on any provider whose Kubernetes meets
  Modelplane's requirements (Dynamic Resource Allocation and a recent Kubernetes
  version), so you can run on the providers below now, ahead of native
  provisioning.
- **Crossplane provider exists.** A Crossplane provider is published for the
  cloud. A cluster provider for the cloud would build on it, so it marks where
  Modelplane can grow next.
{{< /hint >}}

## Clouds and neoclouds

Listed alphabetically, spanning hyperscalers and GPU-specialist neoclouds. Each
runs a managed Kubernetes service with GPU node pools, so the bring-your-own path
covers them all today. Where a Crossplane provider exists, it's the path to
native provisioning.

{{< table >}}
| Provider / service | Accelerators | Provisioning | BYO | Crossplane |
|---|---|---|---|---|
| Alibaba Cloud (ACK) | {{< accel nvidia >}} | Planned | ✓ | {{< repolink "https://github.com/crossplane-contrib/provider-upjet-alibabacloud" "provider-upjet-alibabacloud" "community" >}} |
| AWS (EKS) | {{< accel nvidia >}} {{< accel trainium >}} | ✓ | ✓ | {{< repolink "https://github.com/crossplane-contrib/provider-upjet-aws" "provider-upjet-aws" "community" >}} |
| Civo | {{< accel nvidia >}} | ✓ | ✓ | {{< repolink "https://github.com/upbound/provider-upjet-civo" "provider-civo" "community" >}} |
| CoreWeave (CKS) | {{< accel nvidia >}} | ✓ | ✓ | {{< repolink "https://github.com/upbound/provider-upjet-coreweave" "provider-coreweave" "community" >}} |
| Crusoe (CMK) | {{< accel nvidia >}} {{< accel amd >}} | Planned | ✓ | none yet |
| DigitalOcean (DOKS) | {{< accel nvidia >}} {{< accel amd >}} | Planned | ✓ | {{< repolink "https://github.com/crossplane-contrib/provider-upjet-digitalocean" "provider-upjet-digitalocean" "community" >}} |
| Fluidstack | {{< accel nvidia >}} | Planned | ✓ | none yet |
| Google Cloud (GKE) | {{< accel nvidia >}} {{< accel tpu >}} | ✓ | ✓ | {{< repolink "https://github.com/crossplane-contrib/provider-upjet-gcp" "provider-upjet-gcp" "community" >}} |
| Huawei Cloud (CCE) | {{< accel nvidia >}} {{< accel ascend >}} | Planned | ✓ | {{< repolink "https://github.com/huaweicloud/provider-huaweicloud" "provider-huaweicloud" "alpha" >}} |
| IBM Cloud (IKS) | {{< accel nvidia >}} | Planned | ✓ | none active |
| Lambda | {{< accel nvidia >}} | Planned | ✓ | none yet |
| Linode / Akamai (LKE) | {{< accel nvidia >}} | Planned | ✓ | {{< repolink "https://github.com/linode/provider-linode" "provider-linode" "official" >}} |
| Microsoft Azure (AKS) | {{< accel nvidia >}} | ✓ | ✓ | {{< repolink "https://github.com/crossplane-contrib/provider-upjet-azure" "provider-upjet-azure" "community" >}} |
| Nebius (mk8s) | {{< accel nvidia >}} | ✓ | ✓ | {{< repolink "https://github.com/upbound/provider-upjet-nebius" "provider-upjet-nebius" "official" >}} |
| Oracle Cloud (OKE) | {{< accel nvidia >}} {{< accel amd >}} | Planned | ✓ | {{< repolink "https://github.com/oracle/crossplane-provider-oci" "crossplane-provider-oci" "official" >}} |
| OVHcloud | {{< accel nvidia >}} | Planned | ✓ | {{< repolink "https://github.com/edixos/provider-ovh" "edixos/provider-ovh" "community" >}} |
| Scaleway (Kapsule) | {{< accel nvidia >}} | Planned | ✓ | {{< repolink "https://github.com/scaleway/crossplane-provider-scaleway" "crossplane-provider-scaleway" "official" >}} |
| Tencent Cloud (TKE) | {{< accel nvidia >}} | Planned | ✓ | {{< repolink "https://github.com/crossplane-contrib/provider-tencentcloud" "provider-tencentcloud" "community" >}} |
| Voltage Park | {{< accel nvidia >}} | Planned | ✓ | none yet |
| Vultr (VKE) | {{< accel nvidia >}} {{< accel amd >}} | ✓ | ✓ | {{< repolink "https://github.com/upbound/provider-upjet-vultr" "provider-upjet-vultr" "community" >}} |
{{< /table >}}

{{< hint "note" >}}
**On-premises and bare metal.** Bring an on-prem cluster the same way as any
other: stand up Kubernetes on your own hardware (like NVIDIA DGX BasePOD
or SuperPOD) with NVIDIA Base Command Manager, Run:ai, or your own tooling, then
register it with `source: Existing`. Provisioning it for you is on the roadmap
too. Modelplane can drive NVIDIA Base Command Manager or other bare-metal
Kubernetes provisioners through Crossplane, the same pattern it uses in the
cloud.
{{< /hint >}}

<!-- vale ai-tells.ShipOveruse = NO -->
Native provisioning expands as more Crossplane providers ship; until then, the
bring-your-own path runs Modelplane on any conformant Kubernetes cluster today.
<!-- vale ai-tells.ShipOveruse = YES -->

{{< hint "tip" >}}

Don't see your cloud or neocloud, or want to be added?
[Open an issue](https://github.com/modelplaneai/modelplane/issues/new) and we'll
track it.
{{< /hint >}}

## Cluster providers

Modelplane provisions each cloud's clusters through a cluster provider, a
Crossplane Configuration you [install]({{< ref
"/install#install-a-cluster-provider" >}}) beside Modelplane. A control plane
that only registers existing clusters needs none. Each provider adds the kinds a
provisioned `InferenceCluster` and its `InferenceClass`es name:

{{< table >}}
| Cloud | Package | Cluster config | Node class | Capacity reservation |
|---|---|---|---|---|
| AWS (EKS) | `modelplane-eks` | `EKSClusterConfig` | `EKSNodeClass` | `EKSCapacityReservation` |
| Civo | `modelplane-civo` | `CivoClusterConfig` | `CivoNodeClass` | None |
| CoreWeave (CKS) | `modelplane-coreweave` | `CoreWeaveClusterConfig` | `CoreWeaveNodeClass` | None |
| Google Cloud (GKE) | `modelplane-gke` | `GKEClusterConfig` | `GKENodeClass` | `GKECapacityReservation` |
| Microsoft Azure (AKS) | `modelplane-aks` | `AKSClusterConfig` | `AKSNodeClass` | None |
| Nebius (mk8s) | `modelplane-nebius` | `NebiusClusterConfig` | `NebiusNodeClass` | None |
| Vultr (VKE) | `modelplane-vultr` | `VultrClusterConfig` | `VultrNodeClass` | None |
{{< /table >}}

Modelplane publishes each package as `xpkg.upbound.io/modelplane/<package>`,
with its kinds in the API group `<cloud>.modelplane.ai/v1alpha1`, such as
`gke.modelplane.ai/v1alpha1`.

{{< cardgroup cols="2" >}}
{{< card title="Register a Cluster" href="/platform/inference-cluster/" >}}
Add a cluster to Modelplane, provisioned or bring-your-own.
{{< /card >}}
{{< card title="Define Hardware Classes" href="/platform/inference-class/" >}}
Describe the GPUs and provisioning recipe each node pool uses.
{{< /card >}}
{{< /cardgroup >}}
