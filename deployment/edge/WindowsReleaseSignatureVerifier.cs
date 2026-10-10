using System;
using System.IO;
using System.Security.Cryptography;
using System.Text.RegularExpressions;

namespace Ybb.Edge
{
    // Windows PowerShell 5.1 runs on .NET Framework, which cannot import PEM keys directly.
    public static class WindowsReleaseSignatureVerifier
    {
        private sealed class DerReader
        {
            private readonly byte[] data;
            private readonly int end;
            private int offset;

            public DerReader(byte[] data) : this(data, 0, data.Length) { }

            private DerReader(byte[] data, int offset, int length)
            {
                this.data = data;
                this.offset = offset;
                end = checked(offset + length);
            }

            public bool AtEnd { get { return offset == end; } }

            public byte[] Read(int expectedTag)
            {
                if (offset >= end || data[offset++] != expectedTag)
                    throw new InvalidDataException("Invalid RSA public key DER tag.");
                if (offset >= end) throw new InvalidDataException("Truncated RSA public key.");
                int length = data[offset++];
                if ((length & 0x80) != 0)
                {
                    int count = length & 0x7f;
                    if (count == 0 || count > 4 || count > end - offset)
                        throw new InvalidDataException("Invalid RSA public key DER length.");
                    length = 0;
                    for (int i = 0; i < count; i++) length = checked(length * 256 + data[offset++]);
                }
                if (length < 0 || length > end - offset)
                    throw new InvalidDataException("Truncated RSA public key DER value.");
                byte[] value = new byte[length];
                Buffer.BlockCopy(data, offset, value, 0, length);
                offset += length;
                return value;
            }
        }

        private static byte[] PositiveInteger(DerReader reader)
        {
            byte[] value = reader.Read(0x02);
            if (value.Length == 0 || (value[0] & 0x80) != 0)
                throw new InvalidDataException("Invalid RSA public key integer.");
            if (value.Length > 1 && value[0] == 0)
            {
                byte[] trimmed = new byte[value.Length - 1];
                Buffer.BlockCopy(value, 1, trimmed, 0, trimmed.Length);
                return trimmed;
            }
            return value;
        }

        public static bool Verify(string publicKeyPath, string signaturePath, string contentPath)
        {
            string pem = File.ReadAllText(publicKeyPath);
            Match match = Regex.Match(pem,
                @"\A\s*-----BEGIN PUBLIC KEY-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END PUBLIC KEY-----\s*\z");
            if (!match.Success) throw new InvalidDataException("Expected an RSA PUBLIC KEY PEM file.");
            DerReader outer = new DerReader(Convert.FromBase64String(match.Groups[1].Value));
            DerReader spki = new DerReader(outer.Read(0x30));
            if (!outer.AtEnd) throw new InvalidDataException("Trailing data in RSA public key.");
            DerReader algorithm = new DerReader(spki.Read(0x30));
            byte[] rsaOid = { 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01 };
            byte[] oid = algorithm.Read(0x06);
            if (oid.Length != rsaOid.Length)
                throw new InvalidDataException("Release public key is not RSA.");
            for (int i = 0; i < oid.Length; i++)
                if (oid[i] != rsaOid[i]) throw new InvalidDataException("Release public key is not RSA.");
            if (algorithm.Read(0x05).Length != 0 || !algorithm.AtEnd)
                throw new InvalidDataException("Invalid RSA public key algorithm.");
            byte[] bits = spki.Read(0x03);
            if (!spki.AtEnd || bits.Length < 2 || bits[0] != 0)
                throw new InvalidDataException("Invalid RSA public key bit string.");
            byte[] rsaDer = new byte[bits.Length - 1];
            Buffer.BlockCopy(bits, 1, rsaDer, 0, rsaDer.Length);
            DerReader key = new DerReader(rsaDer);
            DerReader parameters = new DerReader(key.Read(0x30));
            if (!key.AtEnd) throw new InvalidDataException("Trailing data in RSA public key.");
            RSAParameters rsa = new RSAParameters
            {
                Modulus = PositiveInteger(parameters),
                Exponent = PositiveInteger(parameters)
            };
            if (!parameters.AtEnd) throw new InvalidDataException("Trailing RSA public key parameters.");
            using (var provider = new RSACryptoServiceProvider(new CspParameters(24)))
            using (var sha256 = new SHA256CryptoServiceProvider())
            {
                provider.PersistKeyInCsp = false;
                provider.ImportParameters(rsa);
                return provider.VerifyData(File.ReadAllBytes(contentPath), sha256,
                    File.ReadAllBytes(signaturePath));
            }
        }
    }
}
