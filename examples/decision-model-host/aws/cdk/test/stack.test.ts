import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import { test } from "node:test";
import { App, CliCredentialsStackSynthesizer } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { DecisionHostStack } from "../lib/decision-host-stack";
import { loadModels, renderUserData, ROOT } from "../lib/user-data";

const KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleOnlyNotARealKey0000000000000000000 test";

function synth(overrides: Partial<{ operatorCidr: string; model: string }> = {}): Template {
  const app = new App();
  const stack = new DecisionHostStack(app, "Test", {
    env: { region: "us-east-2" },
    synthesizer: new CliCredentialsStackSynthesizer(),
    vpcId: "vpc-0123456789abcdef0",
    operatorCidr: overrides.operatorCidr ?? "203.0.113.7/32",
    model: overrides.model ?? "jevk5-4b",
    instanceType: "g4dn.xlarge",
    publicKey: KEY,
  });
  return Template.fromStack(stack);
}

test("creates no IAM resources", () => {
  const template = synth();
  for (const type of ["AWS::IAM::Role", "AWS::IAM::InstanceProfile", "AWS::IAM::Policy"]) {
    template.resourceCountIs(type, 0);
  }
  template.hasResourceProperties("AWS::EC2::Instance", { IamInstanceProfile: Match.absent() });
});

test("opens only SSH, only to the operator address", () => {
  const template = synth();
  template.resourceCountIs("AWS::EC2::SecurityGroupIngress", 0);
  template.hasResourceProperties("AWS::EC2::SecurityGroup", {
    SecurityGroupIngress: [
      { IpProtocol: "tcp", FromPort: 22, ToPort: 22, CidrIp: "203.0.113.7/32", Description: "operator-ssh" },
    ],
  });
});

test("refuses an operator range wider than one address", () => {
  for (const cidr of ["0.0.0.0/0", "203.0.113.0/24", "not-a-cidr"]) {
    assert.throws(() => synth({ operatorCidr: cidr }), /single \/32/);
  }
});

test("hardens the instance: IMDSv2, encrypted gp3 root, stop on shutdown", () => {
  synth().hasResourceProperties("AWS::EC2::Instance", {
    InstanceType: "g4dn.xlarge",
    InstanceInitiatedShutdownBehavior: "stop",
    MetadataOptions: { HttpTokens: "required", HttpPutResponseHopLimit: 1 },
    BlockDeviceMappings: [
      { DeviceName: "/dev/sda1", Ebs: { VolumeSize: 100, VolumeType: "gp3", Encrypted: true } },
    ],
    ImageId: Match.stringLikeRegexp("^\\{\\{resolve:ssm:/aws/service/deeplearning/"),
  });
});

test("refuses an unknown model", () => {
  assert.throws(() => synth({ model: "nope" }), /unknown model/);
});

test("renders the same user data as the CLI renderer, for every model", () => {
  for (const key of Object.keys(loadModels().models)) {
    const python = execFileSync("python3", [join(ROOT, "aws", "cli", "render_user_data.py"), key], {
      encoding: "utf8",
    });
    assert.equal(renderUserData(key), python, key);
  }
});

test("user data binds the services to loopback and verifies the model checksum", () => {
  const text = renderUserData("jevk5-4b");
  assert.match(text, /--host 127\.0\.0\.1 --port 8080/);
  assert.match(text, /sha256sum -c -/);
  assert.doesNotMatch(text, /0\.0\.0\.0/);
});
