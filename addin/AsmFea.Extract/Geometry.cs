// Units, vectors and transforms. The SolidWorks API works in metres, kg and Pa; the neutral
// model is mm, N, tonne, s, MPa (PLAN.md "Neutral model"). Every conversion goes through here.
using System;

namespace AsmFea.Extract
{
    public static class Units
    {
        public const double M_TO_MM = 1000.0;
        public const double M2_TO_MM2 = 1e6;
        public const double M3_TO_MM3 = 1e9;
        public const double PA_TO_MPA = 1e-6;
        public const double KG_M3_TO_T_MM3 = 1e-12;     // kg/m^3 -> tonne/mm^3
    }

    public struct Vec3
    {
        public double X, Y, Z;
        public Vec3(double x, double y, double z) { X = x; Y = y; Z = z; }
        public static Vec3 From(double[] a, int offset = 0) { return new Vec3(a[offset], a[offset + 1], a[offset + 2]); }

        public static Vec3 operator +(Vec3 a, Vec3 b) { return new Vec3(a.X + b.X, a.Y + b.Y, a.Z + b.Z); }
        public static Vec3 operator -(Vec3 a, Vec3 b) { return new Vec3(a.X - b.X, a.Y - b.Y, a.Z - b.Z); }
        public static Vec3 operator *(Vec3 a, double k) { return new Vec3(a.X * k, a.Y * k, a.Z * k); }
        public double Dot(Vec3 b) { return X * b.X + Y * b.Y + Z * b.Z; }
        public Vec3 Cross(Vec3 b) { return new Vec3(Y * b.Z - Z * b.Y, Z * b.X - X * b.Z, X * b.Y - Y * b.X); }
        public double Length { get { return Math.Sqrt(Dot(this)); } }
        public Vec3 Unit()
        {
            double l = Length;
            if (l < 1e-300) throw new InvalidOperationException("zero-length vector");
            return this * (1.0 / l);
        }
        public double[] ToArray() { return new[] { X, Y, Z }; }

        /// <summary>Rounded for JSON: 1e-6 mm is far below every matching tolerance.</summary>
        public double[] ToRounded(int digits = 6)
        {
            return new[] { Math.Round(X, digits), Math.Round(Y, digits), Math.Round(Z, digits) };
        }
    }

    /// <summary>
    /// A rigid transform, part frame to assembly frame, in mm: p' = R p + t.
    /// </summary>
    public class Xform
    {
        public readonly double[,] R = new double[3, 3];
        public Vec3 T;

        public static Xform Identity()
        {
            var x = new Xform();
            x.R[0, 0] = x.R[1, 1] = x.R[2, 2] = 1.0;
            return x;
        }

        /// <summary>
        /// From SolidWorks MathTransform.ArrayData (16 doubles, metres). SolidWorks transforms row
        /// vectors: x' = x*a0 + y*a3 + z*a6 + a9 (and so on), so element (i, j) of the
        /// column-vector rotation is a[j*3 + i]. a[12] is the scale, which must be 1 for a rigid
        /// component transform. To be confirmed live (milestone 2 test list, item T1).
        /// </summary>
        public static Xform FromSolidWorks(double[] a)
        {
            if (a == null || a.Length < 13) throw new ArgumentException("transform array too short");
            if (Math.Abs(a[12] - 1.0) > 1e-9) throw new ArgumentException("scaled component transform (" + a[12] + ")");
            var x = new Xform();
            for (int i = 0; i < 3; i++)
                for (int j = 0; j < 3; j++)
                    x.R[i, j] = a[j * 3 + i];
            x.T = new Vec3(a[9], a[10], a[11]) * Units.M_TO_MM;
            return x;
        }

        public Vec3 Point(Vec3 p)
        {
            return new Vec3(R[0, 0] * p.X + R[0, 1] * p.Y + R[0, 2] * p.Z + T.X,
                            R[1, 0] * p.X + R[1, 1] * p.Y + R[1, 2] * p.Z + T.Y,
                            R[2, 0] * p.X + R[2, 1] * p.Y + R[2, 2] * p.Z + T.Z);
        }

        /// <summary>Assembly frame back to part frame (R is orthonormal: its inverse is its transpose).</summary>
        public Vec3 InversePoint(Vec3 p)
        {
            var q = p - T;
            return new Vec3(R[0, 0] * q.X + R[1, 0] * q.Y + R[2, 0] * q.Z,
                            R[0, 1] * q.X + R[1, 1] * q.Y + R[2, 1] * q.Z,
                            R[0, 2] * q.X + R[1, 2] * q.Y + R[2, 2] * q.Z);
        }

        public Vec3 Direction(Vec3 d)
        {
            return new Vec3(R[0, 0] * d.X + R[0, 1] * d.Y + R[0, 2] * d.Z,
                            R[1, 0] * d.X + R[1, 1] * d.Y + R[1, 2] * d.Z,
                            R[2, 0] * d.X + R[2, 1] * d.Y + R[2, 2] * d.Z);
        }

        /// <summary>Row-major 4x4 for model.json: translation in elements 3, 7, 11.</summary>
        public double[] ToRowMajor4x4()
        {
            return new[]
            {
                R[0, 0], R[0, 1], R[0, 2], T.X,
                R[1, 0], R[1, 1], R[1, 2], T.Y,
                R[2, 0], R[2, 1], R[2, 2], T.Z,
                0, 0, 0, 1.0,
            };
        }

        /// <summary>Largest deviation of R from orthonormal: a mirrored or skewed transform is refused.</summary>
        public double OrthoError()
        {
            double err = 0;
            for (int i = 0; i < 3; i++)
                for (int j = 0; j < 3; j++)
                {
                    double d = 0;
                    for (int k = 0; k < 3; k++) d += R[k, i] * R[k, j];
                    err = Math.Max(err, Math.Abs(d - (i == j ? 1 : 0)));
                }
            double det = R[0, 0] * (R[1, 1] * R[2, 2] - R[1, 2] * R[2, 1])
                       - R[0, 1] * (R[1, 0] * R[2, 2] - R[1, 2] * R[2, 0])
                       + R[0, 2] * (R[1, 0] * R[2, 1] - R[1, 1] * R[2, 0]);
            return Math.Max(err, Math.Abs(det - 1.0));
        }
    }
}
