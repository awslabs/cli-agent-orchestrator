import { CfnOutput, Fn, Stack, StackProps, Tags } from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";
import { loadModels, renderUserData } from "./user-data";

// The NVIDIA-driver Deep Learning AMI, resolved by CloudFormation at deploy time.
export const AMI_PARAMETER =
  "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id";

export interface DecisionHostProps extends StackProps {
  readonly vpcId: string;
  readonly operatorCidr: string;
  readonly model: string;
  readonly instanceType: string;
  readonly publicKey: string;
}

/**
 * One GPU instance serving a JevK5-family decision model on loopback, reachable only by SSH
 * from the operator's address. L1 resources only: the stack creates no IAM role or profile.
 */
export class DecisionHostStack extends Stack {
  constructor(scope: Construct, id: string, props: DecisionHostProps) {
    super(scope, id, props);
    if (!/^\d{1,3}(\.\d{1,3}){3}\/32$/.test(props.operatorCidr)) {
      throw new Error(`operatorCidr must be a single /32 address, got ${props.operatorCidr}`);
    }
    const model = loadModels().models[props.model];
    if (model === undefined) {
      throw new Error(`unknown model ${props.model}`);
    }

    const securityGroup = new ec2.CfnSecurityGroup(this, "SshOnly", {
      groupDescription: "Decision host: SSH from the operator address only",
      vpcId: props.vpcId,
      securityGroupIngress: [
        {
          ipProtocol: "tcp",
          fromPort: 22,
          toPort: 22,
          cidrIp: props.operatorCidr,
          description: "operator-ssh",
        },
      ],
    });

    const keyPair = new ec2.CfnKeyPair(this, "OperatorKey", {
      keyName: `${this.stackName}-operator`,
      publicKeyMaterial: props.publicKey.trim(),
    });

    const instance = new ec2.CfnInstance(this, "Host", {
      imageId: `{{resolve:ssm:${AMI_PARAMETER}}}`,
      instanceType: props.instanceType,
      keyName: keyPair.keyName,
      securityGroupIds: [securityGroup.attrGroupId],
      instanceInitiatedShutdownBehavior: "stop",
      metadataOptions: { httpTokens: "required", httpEndpoint: "enabled", httpPutResponseHopLimit: 1 },
      blockDeviceMappings: [
        {
          deviceName: "/dev/sda1",
          ebs: { volumeSize: 100, volumeType: "gp3", encrypted: true, deleteOnTermination: true },
        },
      ],
      userData: Fn.base64(renderUserData(props.model)),
      tags: [
        { key: "Name", value: this.stackName },
        { key: "Model", value: model.name },
      ],
    });
    instance.node.addDependency(keyPair);

    Tags.of(this).add("Project", "cao-decision-host");

    new CfnOutput(this, "InstanceId", { value: instance.ref });
    new CfnOutput(this, "PublicIp", { value: instance.attrPublicIp });
    new CfnOutput(this, "Model", { value: model.name });
    new CfnOutput(this, "Tunnel", {
      value: Fn.join("", [
        "ssh -i keys/decision-host -N -L 127.0.0.1:18090:127.0.0.1:8090 ubuntu@",
        instance.attrPublicIp,
      ]),
    });
  }
}
