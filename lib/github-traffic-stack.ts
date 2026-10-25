import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as targets from 'aws-cdk-lib/aws-scheduler-targets';
import { Construct } from 'constructs';

const root = path.join(import.meta.dirname, '..');

export class GithubTrafficStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const tokenParameterName: string = this.node.getContext('githubTokenParameterName');
    const scheduleExpression: string = this.node.getContext('scheduleExpression');

    // 収集した履歴そのもの。GitHub 側からは消えるデータなので、スタックを消しても残す
    const table = new dynamodb.TableV2(this, 'TrafficTable', {
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ダッシュボードの HTML と集計 JSON。集計 JSON は DynamoDB から作り直せるので消してよい
    const siteBucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const collector = new lambda.Function(this, 'Collector', {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(root, 'lambda/collector')),
      timeout: cdk.Duration.minutes(5),
      memorySize: 256,
      environment: {
        TABLE_NAME: table.tableName,
        BUCKET_NAME: siteBucket.bucketName,
        TOKEN_PARAMETER_NAME: tokenParameterName,
      },
      logGroup: new logs.LogGroup(this, 'CollectorLogs', {
        retention: logs.RetentionDays.THREE_MONTHS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    table.grantReadWriteData(collector);
    siteBucket.grantPut(collector, 'data/*');
    // トークンの SecureString は CloudFormation では作れないので、デプロイ後にスクリプトで入れる
    collector.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['ssm:GetParameter'],
        resources: [
          this.formatArn({
            service: 'ssm',
            resource: 'parameter',
            resourceName: tokenParameterName.replace(/^\//, ''),
          }),
        ],
      }),
    );

    new scheduler.Schedule(this, 'DailySchedule', {
      schedule: scheduler.ScheduleExpression.expression(scheduleExpression),
      target: new targets.LambdaInvoke(collector),
      description: 'GitHub のトラフィックを毎日収集する',
    });

    // Basic 認証の資格情報は KeyValueStore に置き、テンプレートにもコードにも書かない
    const authStore = new cloudfront.KeyValueStore(this, 'AuthStore');
    const authFunction = new cloudfront.Function(this, 'BasicAuth', {
      code: cloudfront.FunctionCode.fromFile({ filePath: path.join(root, 'functions/basic-auth.js') }),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      keyValueStore: authStore,
    });

    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        functionAssociations: [
          { function: authFunction, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST },
        ],
      },
      priceClass: cloudfront.PriceClass.PRICE_CLASS_200,
    });

    new s3deploy.BucketDeployment(this, 'DeploySite', {
      sources: [s3deploy.Source.asset(path.join(root, 'web'))],
      destinationBucket: siteBucket,
      // Lambda が書く集計 JSON を消さない
      exclude: ['data/*'],
      cacheControl: [s3deploy.CacheControl.maxAge(cdk.Duration.minutes(5))],
      distribution,
    });

    new cdk.CfnOutput(this, 'DashboardUrl', { value: `https://${distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, 'AuthStoreArn', { value: authStore.keyValueStoreArn });
    new cdk.CfnOutput(this, 'CollectorFunctionName', { value: collector.functionName });
    new cdk.CfnOutput(this, 'TokenParameterName', { value: tokenParameterName });
  }
}
