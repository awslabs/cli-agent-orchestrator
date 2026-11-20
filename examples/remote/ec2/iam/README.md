# The instance role and the backup bucket: check before use

Every agent on the box can use the instance role, through the instance
metadata service. So the role is the main limit on what an agent can do in
your AWS account. Keep it as small as these two files.

> **Check before use.** The ARN forms below follow the AWS docs as read for
> this sample, but they were not tested against a live account. Before you
> rely on them, check them against the Bedrock, Systems Manager and S3 docs,
> or with the IAM policy simulator.

## [`instance-role-policy.json.example`](instance-role-policy.json.example)

| Statement | What it allows | What to check |
|---|---|---|
| `SessionManagerOnly` | `ssm:UpdateInstanceInformation`, `ssmmessages:*`, `ec2messages:*`: what the SSM Agent needs for Session Manager. It is used **instead of** the managed `AmazonSSMManagedInstanceCore`, which also allows `ssm:GetParameter(s)` on every parameter, so any agent could read Parameter Store. | Newer SSM Agent versions may not need `ec2messages:*`. Nothing here grants Parameter Store, Run Command documents or `ssm:SendCommand`. |
| `InvokeNamedProfiles` | Invoke the three `au.` and the three `global.` inference profiles, in `ap-southeast-2`. | Delete the profiles you don't use. Global profiles may run your requests in any Region. |
| `InvokeModelsOnlyThroughThoseProfiles` | The foundation models behind those profiles: `ap-southeast-2` and `ap-southeast-4` for `au.`, and the Region-less ARN for `global.`. The `bedrock:InferenceProfileArn` condition only allows them through the named profiles, not as direct calls. | The exact model ids (some have a version suffix), the Region-less ARN form for global profiles, and the condition key's behaviour. |
| `WriteAndReadBackupsOnly` | `s3:PutObject` and `s3:GetObject` on `<backup-bucket>/cao-box/*` only. There is no delete and no list. | Put needs `kms:GenerateDataKey`; get (restore) needs `kms:Decrypt`. |
| `BackupKey` | The backup bucket's KMS key, and nothing else. | The key policy must also allow the role. |

There is nothing here for IAM, EC2, Secrets Manager, Parameter Store or
other buckets. Keep it that way.

## [`backup-bucket-policy.json.example`](backup-bucket-policy.json.example)

The backups hold CAO's database, which contains message text (inbox,
handoff results, launch records and memory metadata). The bucket policy
refuses:

- any request that isn't over TLS;
- any upload that doesn't ask for SSE-KMS (`backup.sh` always asks for it);
- any upload that asks for a different KMS key.

Apply it with
`aws s3api put-bucket-policy --bucket <backup-bucket> --policy file://iam/backup-bucket-policy.json.example`
after filling in the placeholders.
