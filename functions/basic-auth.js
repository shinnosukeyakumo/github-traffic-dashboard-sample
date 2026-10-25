// Basic 認証。正解の "user:password" の Base64 は KeyValueStore の basic-auth キーに置く
import cf from 'cloudfront';

const kvs = cf.kvs();

async function handler(event) {
  const request = event.request;
  const header = request.headers.authorization;

  let expected = null;
  try {
    expected = await kvs.get('basic-auth');
  } catch (e) {
    // キー未設定のときは誰も通さない
  }

  if (expected && header && header.value === 'Basic ' + expected) {
    return request;
  }

  return {
    statusCode: 401,
    statusDescription: 'Unauthorized',
    headers: { 'www-authenticate': { value: 'Basic realm="GitHub Traffic"' } },
  };
}
