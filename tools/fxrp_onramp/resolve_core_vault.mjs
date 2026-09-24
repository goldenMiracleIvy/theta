// resolve_core_vault.mjs — read-only pre-flight for the FXRP mint.
//
// Resolves, live from the Flare contract registry, everything the mint needs:
//   * AssetManagerFXRP
//   * the Core Vault XRPL address you must pay
//   * the FXRP token address on Flare
//   * the current direct-minting fee settings
//
// No state-changing call, no key, no network write. Run this first to see what
// the mint would cost before committing any XRP.
//
// Usage:
//   node resolve_core_vault.mjs                 # mainnet
//   FLARE_NETWORK=coston2 node resolve_core_vault.mjs
import { createPublicClient, http } from "viem";
import { flare as flareMainnet, coston2 } from "@flarenetwork/flare-wagmi-periphery-package";

const REGISTRY = "0xaD67FE66660Fb8dFE9d6b1b4240d8650e30F6019";
const UBA_PER_XRP = 1_000_000;

const network = (process.env.FLARE_NETWORK || "mainnet").toLowerCase();
const chain = network === "coston2" ? coston2 : flareMainnet;
const rpcUrl =
  process.env.FLARE_RPC_URL ||
  (network === "coston2"
    ? "https://coston2-api.flare.network/ext/C/rpc"
    : "https://flare-api.flare.network/ext/C/rpc");

const pc = createPublicClient({ chain, transport: http(rpcUrl) });

const assetManager = await pc.readContract({
  address: REGISTRY,
  abi: chain.iFlareContractRegistryAbi,
  functionName: "getContractAddressByName",
  args: ["AssetManagerFXRP"],
});

const read = (fn, abi = chain.iDirectMintingSettingsAbi) =>
  pc.readContract({ address: assetManager, abi, functionName: fn });

const coreVault = await pc.readContract({
  address: assetManager,
  abi: chain.iDirectMintingAbi,
  functionName: "directMintingPaymentAddress",
});
const fxrpToken = await read("fAsset", chain.iAssetManagerAbi);

const [bips, minFeeUBA, executorFeeUBA] = await Promise.all([
  read("getDirectMintingFeeBIPS"),
  read("getDirectMintingMinimumFeeUBA"),
  read("getDirectMintingExecutorFeeUBA"),
]);

console.log(`=== FXRP on-ramp — ${network} ===`);
console.log("AssetManagerFXRP   :", assetManager);
console.log("Core Vault (XRPL)  :", coreVault);
console.log("FXRP token (Flare) :", fxrpToken);
console.log("");
console.log("fee                :", `${bips} bips`);
console.log("minimum fee        :", `${Number(minFeeUBA) / UBA_PER_XRP} XRP`);
console.log("executor fee       :", `${Number(executorFeeUBA) / UBA_PER_XRP} XRP`);
console.log("");
console.log("Payment = net + max(net x bips/10000, minimum fee) + executor fee");
for (const net of [10, 30, 100]) {
  const mintFee = Math.max((net * Number(bips)) / 10_000, Number(minFeeUBA) / UBA_PER_XRP);
  const total = net + mintFee + Number(executorFeeUBA) / UBA_PER_XRP;
  console.log(`  convert ${String(net).padStart(4)} XRP  ->  pay ${total.toFixed(6)} XRP`);
}
