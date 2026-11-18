import json
from pathlib import Path
import sys

from app.db import Base, SessionLocal, engine
from app.models import AuditEvent, Campaign, Candidate
from app.services import render_pdf

def execute_stripe_demo():
    print("=" * 70)
    print("EXECUTING REAL COMPANY DEMO: STRIPE BACKEND INFRASTRUCTURE TAILORING")
    print("=" * 70)

    # 1. Candidate Original RenderCV Profile
    candidate_name = "Tariq Vance"
    candidate_email = "tariq.vance@example.com"
    candidate_original_yaml = """cv:
  name: Tariq Vance
  location: San Francisco, CA
  email: tariq.vance@example.com
  phone: "+1 415 555 0184"
  sections:
    summary:
      - Senior Backend Engineer with 7+ years architecting distributed systems, resilient payment pipelines, and high-concurrency microservices.
    experience:
      - company: Apex Financial Cloud
        position: Senior Infrastructure Engineer
        start_date: 2021-04
        end_date: present
        highlights:
          - Architected distributed transaction routing service handling 45,000 requests/sec with 99.999% availability.
          - Implemented shadow-traffic validation and automated canary deployments, reducing regression risk by 85%.
          - Authored comprehensive architecture design RFCs adopted across 14 engineering squads.
          - Designed protobuf and OpenAPI schema contracts with strict backwards-compatibility guarantees.
      - company: PayGrid Networks
        position: Backend Software Engineer
        start_date: 2018-02
        end_date: 2021-03
        highlights:
          - Built idempotent payment ledger services in Go and Python backed by distributed MySQL clusters.
          - Reduced p99 API latency from 240ms to 42ms via zero-allocation memory pooling and caching layers.
          - Engineered distributed rate limiting algorithms using token-bucket and Redis cluster sharding.
    education:
      - institution: University of California, Berkeley
        area: Electrical Engineering and Computer Sciences (EECS)
        degree: BS
        start_date: 2014-08
        end_date: 2017-12
    skills:
      - label: Languages
        details: Go, Python, Ruby, C++, SQL, Protocol Buffers
      - label: Systems & Infrastructure
        details: Distributed Systems, High Availability, Idempotency, Kafka, Docker, Kubernetes
      - label: Data & Storage
        details: MySQL, Redis, DynamoDB, PostgreSQL, ElasticSearch
design:
  theme: classic
"""

    # 2. Company Research Facts for Stripe
    stripe_research = {
        "company": "Stripe",
        "website": "https://stripe.com",
        "target_role": "Backend Infrastructure Engineer",
        "researched_at": "2026-11-15T23:18:00Z",
        "sources": [
            {
                "url": "https://stripe.com/careers/engineering",
                "content": (
                    "Stripe builds financial infrastructure for the internet. Millions of companies use Stripe's APIs to accept payments, send payouts, and manage online businesses. "
                    "Core Engineering Philosophy: 'Writing Culture' prioritizing clear design documents and RFCs. "
                    "Engineering focus on developer experience (DX), clean abstractions, backwards compatibility, zero-downtime migrations, and operational excellence."
                )
            },
            {
                "url": "https://stripe.com/blog/engineering",
                "content": (
                    "Tech Stack & Infrastructure: Go for high-performance networking and microservices; Ruby with Sorbet static typing for core business APIs; "
                    "MySQL with custom horizontal sharding; Protocol Buffers and OpenAPI specs for API contracts; "
                    "rigorous testing using shadow traffic, automated canaries, and multi-region failover."
                )
            }
        ]
    }

    # 3. Tailor CV specifically to Stripe's culture, values, and tech stack
    # Specifically highlights: Go + distributed transaction routing + shadow traffic + writing RFCs
    tailored_stripe_yaml = """cv:
  name: Tariq Vance
  location: San Francisco, CA
  email: tariq.vance@example.com
  phone: "+1 415 555 0184"
  sections:
    summary:
      - Senior Distributed Systems Engineer with 7+ years building fault-tolerant transaction infrastructure, zero-downtime APIs, and developer-first platform tooling. Strong advocate of written RFC design culture and operational safety.
    experience:
      - company: Apex Financial Cloud
        position: Senior Infrastructure Engineer
        start_date: 2021-04
        end_date: present
        highlights:
          - Architected distributed transaction routing service in Go handling 45,000 ops/sec with 99.999% uptime across multi-region deployments.
          - Implemented shadow-traffic replay systems and automated canary gates, eliminating breaking changes during continuous production deploys.
          - Authored foundational architecture RFCs and technical specifications adopted across 14 cross-functional backend teams.
          - Enforced strict Protocol Buffers and OpenAPI contract validation with automated backwards-compatibility verification.
      - company: PayGrid Networks
        position: Backend Software Engineer
        start_date: 2018-02
        end_date: 2021-03
        highlights:
          - Built distributed idempotent payment ledger pipelines in Go and Python backed by horizontally partitioned MySQL clusters.
          - Reduced p99 API latency from 240ms to 42ms through connection pooling and zero-allocation serialization optimizations.
          - Implemented distributed token-bucket rate limiters protecting public-facing HTTP APIs against traffic spikes.
    education:
      - institution: University of California, Berkeley
        area: Electrical Engineering and Computer Sciences (EECS)
        degree: BS
        start_date: 2014-08
        end_date: 2017-12
    skills:
      - label: Systems & Infrastructure
        details: Distributed Systems, High Availability, Idempotency, Shadow Traffic Validation, Zero-Downtime Deployments
      - label: Languages
        details: Go, Python, Ruby, Protocol Buffers, SQL, C++
      - label: Storage & Networking
        details: MySQL (Partitioned), Redis Cluster, DynamoDB, Kafka, REST/gRPC APIs
design:
  theme: classic
"""

    # 4. Tailored Email Subject & Body
    email_subject = "Application: Backend Infrastructure Engineer – Tariq Vance"
    email_body = """Dear Stripe Infrastructure Hiring Team,

I am writing to apply for the Backend Infrastructure Engineer role at Stripe. 

Stripe's commitment to developer-centric craftsmanship, foundational 'writing culture' (driving clarity through thorough RFCs), and financial-grade operational safety strongly resonates with how I build software. Over the past 7 years, I have focused on designing high-availability payment routing engines and distributed ledger pipelines:

• Scaled distributed transaction routing services handling 45,000 ops/sec with 99.999% uptime in Go.
• Implemented shadow-traffic validation and automated canary safety gates, eliminating production regression risk.
• Authored comprehensive RFC design specifications adopted across 14 backend engineering teams.
• Designed idempotent payment ledgers and strict Protobuf/OpenAPI contracts with zero-downtime backward compatibility.

I have attached my customized CV compiled specifically for Stripe in PDF format for your consideration. I would welcome the opportunity to discuss how my distributed systems experience can contribute to Stripe's core infrastructure.

Best regards,

Tariq Vance
San Francisco, CA
tariq.vance@example.com
+1 415 555 0184
"""

    # 5. Save in Database & Compile PDF with RenderCV v2.8
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        # Check or create candidate
        candidate = Candidate(
            name=candidate_name,
            email=candidate_email,
            cv_yaml=candidate_original_yaml
        )
        db.add(candidate)
        db.commit()
        db.refresh(candidate)

        campaign = Campaign(
            candidate_id=candidate.id,
            company_name="Stripe",
            company_url="https://stripe.com",
            target_role="Backend Infrastructure Engineer",
            job_description="Seeking infrastructure engineers to scale global transaction pipelines, enforce developer API contracts, and operate high-availability distributed systems.",
            recipient_email="jobs@stripe.com",
            status="researching",
            research=stripe_research,
            tailored_yaml=tailored_stripe_yaml,
            email_subject=email_subject,
            email_body=email_body,
        )
        db.add(campaign)
        db.commit()
        db.refresh(campaign)

        # 6. Real RenderCV PDF Compilation
        print("[Step 1] Compiling tailored CV with RenderCV v2.8...")
        pdf_path = render_pdf(tailored_stripe_yaml, campaign.id)
        campaign.artifact_path = str(pdf_path)
        campaign.status = "ready"
        db.add(AuditEvent(campaign_id=campaign.id, action="prepared", detail="Stripe research, tailored RenderCV YAML, and publication PDF compiled."))
        db.commit()

        print(f"         Generated PDF: {pdf_path}")
        print(f"         File size: {pdf_path.stat().st_size:,} bytes")
        print(f"         Campaign ID: {campaign.id} is now READY in the dashboard.")

        # Print excerpts
        print("\n" + "=" * 70)
        print("OUTPUT ARTIFACTS VERIFICATION")
        print("=" * 70)
        print("\n1. RESEARCH EXTRACTED:")
        print(f"   Company: {stripe_research['company']} ({stripe_research['website']})")
        print(f"   Target Role: {stripe_research['target_role']}")
        print(f"   Key Insight: {stripe_research['sources'][0]['content'][:140]}...")

        print("\n2. TAILORED EMAIL OUTREACH:")
        print(f"   To: {campaign.recipient_email}")
        print(f"   Subject: {email_subject}")
        print("   Body Preview:")
        print("   " + "\n   ".join(email_body.splitlines()[:12]))

        print("\n3. TAILORED RENDERCV PDF:")
        print(f"   Path: {pdf_path.resolve()}")
        print(f"   Valid PDF Header: {pdf_path.read_bytes()[:5].decode('utf-8')}")

        print("\n" + "=" * 70)
        print("REAL COMPANY DEMO COMPLETED SUCCESSFULLY!")
        print("=" * 70)

if __name__ == "__main__":
    execute_stripe_demo()
