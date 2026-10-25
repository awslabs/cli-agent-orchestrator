import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { App, CliCredentialsStackSynthesizer } from "aws-cdk-lib";
import { DecisionHostStack } from "../lib/decision-host-stack";

const app = new App();
const ctx = (key: string): string => {
  const value = app.node.tryGetContext(key);
  if (typeof value !== "string" || value === "") {
    throw new Error(`missing context value ${key} (cdk.json or -c ${key}=...)`);
  }
  return value;
};

new DecisionHostStack(app, "CaoDecisionHost", {
  env: { region: ctx("region") },
  // Deploy with the caller's own credentials: no bootstrap roles to assume, and no asset bucket.
  synthesizer: new CliCredentialsStackSynthesizer(),
  vpcId: ctx("vpcId"),
  operatorCidr: ctx("operatorCidr"),
  model: ctx("model"),
  instanceType: ctx("instanceType"),
  publicKey: readFileSync(resolve(__dirname, "..", ctx("publicKeyFile")), "utf8"),
});
