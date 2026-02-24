"""
greeks.py — Black-Scholes Greeks engine
IV via Newton-Raphson, full Greeks: delta, gamma, theta, vega
"""
import numpy as np
from scipy.stats import norm

class BSGreeks:
    @staticmethod
    def d1(S, K, T, r, sigma):
        return (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))

    @staticmethod
    def d2(S, K, T, r, sigma):
        return BSGreeks.d1(S, K, T, r, sigma) - sigma * np.sqrt(T)

    @classmethod
    def price(cls, S, K, T, r, sigma, opt_type="CE"):
        d1, d2 = cls.d1(S,K,T,r,sigma), cls.d2(S,K,T,r,sigma)
        if opt_type == "CE":
            return S * norm.cdf(d1) - K * np.exp(-r*T) * norm.cdf(d2)
        return K * np.exp(-r*T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    @classmethod
    def delta(cls, S, K, T, r, sigma, opt_type="CE"):
        d1 = cls.d1(S,K,T,r,sigma)
        return norm.cdf(d1) if opt_type == "CE" else norm.cdf(d1) - 1

    @classmethod
    def gamma(cls, S, K, T, r, sigma):
        d1 = cls.d1(S,K,T,r,sigma)
        return norm.pdf(d1) / (S * sigma * np.sqrt(T))

    @classmethod
    def theta(cls, S, K, T, r, sigma, opt_type="CE"):
        d1, d2 = cls.d1(S,K,T,r,sigma), cls.d2(S,K,T,r,sigma)
        t1 = -(S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T))
        if opt_type == "CE":
            return (t1 - r * K * np.exp(-r*T) * norm.cdf(d2)) / 365
        return (t1 + r * K * np.exp(-r*T) * norm.cdf(-d2)) / 365

    @classmethod
    def vega(cls, S, K, T, r, sigma):
        d1 = cls.d1(S,K,T,r,sigma)
        return S * norm.pdf(d1) * np.sqrt(T) / 100

    @classmethod
    def implied_volatility(cls, market_price, S, K, T, r, opt_type="CE",
                           tol=1e-5, max_iter=200) -> float:
        """Newton-Raphson IV solver"""
        if market_price <= 0:
            return 0.0
        sigma = 0.20  # initial guess
        for _ in range(max_iter):
            try:
                p    = cls.price(S, K, T, r, sigma, opt_type)
                vega = cls.vega(S, K, T, r, sigma) * 100  # unscale
                if abs(vega) < 1e-10:
                    break
                sigma -= (p - market_price) / vega
                sigma  = max(0.001, min(sigma, 5.0))
                if abs(cls.price(S, K, T, r, sigma, opt_type) - market_price) < tol:
                    break
            except Exception:
                break
        return sigma

    @classmethod
    def full_greeks(cls, S, K, T, r, market_price, opt_type="CE") -> dict:
        """Compute all Greeks from market price"""
        iv = cls.implied_volatility(market_price, S, K, T, r, opt_type)
        if iv <= 0:
            return {"iv": 0, "delta": 0, "gamma": 0, "theta": 0, "vega": 0, "fair_price": 0}
        return {
            "iv":         round(iv * 100, 2),          # as percentage
            "delta":      round(cls.delta(S,K,T,r,iv,opt_type), 4),
            "gamma":      round(cls.gamma(S,K,T,r,iv), 5),
            "theta":      round(cls.theta(S,K,T,r,iv,opt_type), 2),
            "vega":       round(cls.vega(S,K,T,r,iv), 4),
            "fair_price": round(cls.price(S,K,T,r,iv,opt_type), 2),
        }
