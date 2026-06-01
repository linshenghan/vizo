#!/usr/bin/env python3
"""
企业微信消息加解密模块（Python 3 版本）
用于验证企业微信回调 URL
"""

import base64
import hashlib
import random
import socket
import struct
import time
import xml.etree.ElementTree as ET
from Crypto.Cipher import AES

# 错误码定义
WXBizMsgCrypt_OK = 0
WXBizMsgCrypt_ValidateSignature_Error = -40001
WXBizMsgCrypt_ParseXml_Error = -40002
WXBizMsgCrypt_ComputeSignature_Error = -40003
WXBizMsgCrypt_IllegalAesKey = -40004
WXBizMsgCrypt_ValidateCorpid_Error = -40005
WXBizMsgCrypt_EncryptAES_Error = -40006
WXBizMsgCrypt_DecryptAES_Error = -40007
WXBizMsgCrypt_IllegalBuffer = -40008
WXBizMsgCrypt_EncodeBase64_Error = -40009
WXBizMsgCrypt_DecodeBase64_Error = -40010


class PKCS7Encoder:
    """PKCS7 填充"""
    block_size = 32

    @classmethod
    def encode(cls, text):
        text_length = len(text)
        amount_to_pad = cls.block_size - (text_length % cls.block_size)
        if amount_to_pad == 0:
            amount_to_pad = cls.block_size
        pad = chr(amount_to_pad)
        return text + pad * amount_to_pad

    @classmethod
    def decode(cls, decrypted):
        pad = ord(decrypted[-1])
        if pad < 1 or pad > 32:
            pad = 0
        return decrypted[:-pad]


class Prpcrypt:
    """AES 加解密"""

    def __init__(self, key):
        self.key = key
        self.mode = AES.MODE_CBC

    def encrypt(self, text, corpid):
        """加密"""
        try:
            # 16位随机字符串 + 4位msg长度(网络字节序) + msg + corpid
            random_str = ''.join(random.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(16))
            text = random_str + struct.pack("I", socket.htonl(len(text.encode()))).decode('latin-1') + text + corpid
            text = PKCS7Encoder.encode(text)

            iv = self.key[:16]
            cryptor = AES.new(self.key, self.mode, iv)
            ciphertext = cryptor.encrypt(text.encode())
            return WXBizMsgCrypt_OK, base64.b64encode(ciphertext).decode()
        except Exception:
            return WXBizMsgCrypt_EncryptAES_Error, None

    def decrypt(self, text, corpid):
        """解密"""
        try:
            ciphertext = base64.b64decode(text)
            iv = self.key[:16]
            cryptor = AES.new(self.key, self.mode, iv)
            plain_bytes = cryptor.decrypt(ciphertext)

            # PKCS7 去 padding（基于字节操作，更可靠）
            pad = plain_bytes[-1]
            if 1 <= pad <= 32:
                plain_bytes = plain_bytes[:-pad]

            # 解析内容（字节级操作）
            # 16字节随机数 + 4字节消息长度 + 消息 + CorpID
            content_bytes = plain_bytes[16:]
            xml_len = socket.ntohl(struct.unpack("I", content_bytes[:4])[0])
            xml_content = content_bytes[4:xml_len + 4].decode('utf-8')
            from_corpid = content_bytes[xml_len + 4:].decode('utf-8')

            if from_corpid != corpid:
                return WXBizMsgCrypt_ValidateCorpid_Error, None
            return WXBizMsgCrypt_OK, xml_content
        except Exception as e:
            print(f"[DEBUG] decrypt exception: {e}")
            return WXBizMsgCrypt_DecryptAES_Error, None


class WXBizMsgCrypt:
    """企业微信消息加解密"""

    def __init__(self, token, encoding_aes_key, corpid):
        try:
            self.key = base64.b64decode(encoding_aes_key + "=")
            assert len(self.key) == 32
        except Exception:
            raise Exception("Invalid EncodingAESKey")
        self.token = token
        self.corpid = corpid

    def _compute_signature(self, token, timestamp, nonce, encrypt):
        """计算签名"""
        sort_list = [token, timestamp, nonce, encrypt]
        sort_list.sort()
        sha = hashlib.sha1()
        sha.update("".join(sort_list).encode())
        return sha.hexdigest()

    def VerifyURL(self, msg_signature, timestamp, nonce, echostr):
        """
        验证 URL 有效性

        Args:
            msg_signature: 签名
            timestamp: 时间戳
            nonce: 随机数
            echostr: 加密的随机字符串

        Returns:
            (ret, reply_echostr): ret=0 表示成功，reply_echostr 为解密后的明文
        """
        signature = self._compute_signature(self.token, timestamp, nonce, echostr)
        if signature != msg_signature:
            return WXBizMsgCrypt_ValidateSignature_Error, None

        pc = Prpcrypt(self.key)
        ret, reply_echostr = pc.decrypt(echostr, self.corpid)
        return ret, reply_echostr

    def EncryptMsg(self, reply_msg, nonce, timestamp=None):
        """加密消息"""
        pc = Prpcrypt(self.key)
        ret, encrypt = pc.encrypt(reply_msg, self.corpid)
        if ret != WXBizMsgCrypt_OK:
            return ret, None

        if timestamp is None:
            timestamp = str(int(time.time()))
        signature = self._compute_signature(self.token, timestamp, nonce, encrypt)

        resp_xml = f"""<xml>
<Encrypt><![CDATA[{encrypt}]]></Encrypt>
<MsgSignature><![CDATA[{signature}]]></MsgSignature>
<TimeStamp>{timestamp}</TimeStamp>
<Nonce><![CDATA[{nonce}]]></Nonce>
</xml>"""
        return WXBizMsgCrypt_OK, resp_xml

    def DecryptMsg(self, post_data, msg_signature, timestamp, nonce):
        """解密消息"""
        try:
            xml_tree = ET.fromstring(post_data)
            encrypt = xml_tree.find("Encrypt").text
        except Exception:
            return WXBizMsgCrypt_ParseXml_Error, None

        signature = self._compute_signature(self.token, timestamp, nonce, encrypt)
        if signature != msg_signature:
            return WXBizMsgCrypt_ValidateSignature_Error, None

        pc = Prpcrypt(self.key)
        ret, xml_content = pc.decrypt(encrypt, self.corpid)
        return ret, xml_content
