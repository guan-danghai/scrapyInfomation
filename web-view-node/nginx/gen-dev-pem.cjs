/**
 * 自签 PEM（开发用），无需系统安装 openssl。
 * 用法：node gen-dev-pem.cjs <输出目录>
 * 生成：privkey.pem、fullchain.pem
 */
const selfsigned = require('selfsigned');
const fs = require('fs');
const path = require('path');

const outDir = process.argv[2];
if (!outDir) {
  console.error('usage: node gen-dev-pem.cjs <output_dir>');
  process.exit(1);
}

const attrs = [{ name: 'commonName', value: 'ztb.resoftcss.com.cn' }];
const opts = {
  keySize: 2048,
  days: 825,
  algorithm: 'sha256',
  extensions: [
    {
      name: 'subjectAltName',
      altNames: [
        { type: 2, value: 'ztb.resoftcss.com.cn' },
        { type: 2, value: 'localhost' },
        { type: 7, ip: '127.0.0.1' },
      ],
    },
  ],
};

const pems = selfsigned.generate(attrs, opts);
fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(path.join(outDir, 'privkey.pem'), pems.private, 'utf8');
fs.writeFileSync(path.join(outDir, 'fullchain.pem'), pems.cert, 'utf8');
console.log('OK:', path.join(outDir, 'fullchain.pem'));
