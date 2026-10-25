#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { GithubTrafficStack } from '../lib/github-traffic-stack.ts';

const app = new cdk.App();
new GithubTrafficStack(app, 'GithubTrafficStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    // プロファイルの既定リージョンに引きずられないよう、明示しなければ東京にする
    region: process.env.DEPLOY_REGION ?? 'ap-northeast-1',
  },
});
