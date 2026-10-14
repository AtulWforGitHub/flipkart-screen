"""
Bank Offers Extractor Module
Extracts bank offers, cashback, and discount information from Flipkart product pages.
"""

import re
import time
from selenium import webdriver
from selenium.webdriver.common.by import By


_HEADER_XPATH = "//*[contains(text(), 'Bank offer') or contains(text(), 'Bank Offer')]"


def _wait_for_offers_rendered(driver: webdriver.Chrome, max_wait: float = 2.5) -> bool:
    """Wait until the bank-offer section has actually rendered offer text.

    Replaces a fixed 2s sleep: returns as soon as the section shows an
    offer-looking line (rupee amount + 'off'), and still gives up after
    max_wait seconds so the worst case is about the same as before."""
    end = time.time() + max_wait
    while time.time() < end:
        try:
            headers = driver.find_elements(By.XPATH, _HEADER_XPATH)
            if headers:
                container = headers[0].find_element(By.XPATH, "./ancestor::div[6]")
                txt = container.text
                if '₹' in txt and 'off' in txt.lower():
                    return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def _wait_for_text_change(element, before_text: str, max_wait: float = 1.5) -> bool:
    """Wait until element.text differs from before_text. Replaces a fixed 1.5s
    sleep after each carousel click: returns the moment new offers render, and
    returns False if nothing changed (carousel already at its end)."""
    end = time.time() + max_wait
    while time.time() < end:
        try:
            if element.text != before_text:
                return True
        except Exception:
            return True  # element went stale -> DOM re-rendered, treat as changed
        time.sleep(0.1)
    return False


def extract_bank_offers(driver: webdriver.Chrome, html: str, verbose: bool = False) -> list:
    """
    Extract bank offers from Flipkart product page.
    
    Args:
        driver: Selenium WebDriver instance
        html: Page HTML source
        verbose: Print extraction progress
        
    Returns:
        List of bank offer strings
    """
    offers = []
    seen_offers = set()
    
    try:
        # Wait for bank offers to render (returns early once they are visible)
        _wait_for_offers_rendered(driver)
        
        # Method 1: Scroll through carousel and extract all offers
        if verbose:
            print("  [Bank Offers] Extracting from carousel...")
        
        try:
            bank_offer_headers = driver.find_elements(By.XPATH, 
                "//*[contains(text(), 'Bank offer') or contains(text(), 'Bank Offer')]")
            
            if bank_offer_headers:
                for header in bank_offer_headers[:1]:
                    try:
                        # Find carousel container
                        carousel_container = header.find_element(By.XPATH, "./ancestor::div[6]")
                        
                        # Scroll carousel container into view
                        driver.execute_script("arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});", carousel_container)
                        time.sleep(0.2)
                        
                        # Find next/arrow buttons
                        next_buttons = carousel_container.find_elements(By.XPATH, 
                            ".//button[contains(@aria-label, 'next') or contains(@aria-label, 'Next')] | "
                            ".//div[@role='button'][.//svg] | "
                            ".//*[contains(@class, 'next') or contains(@class, 'arrow') or contains(@class, 'right')]//ancestor::button[1]")
                        
                        if verbose:
                            print(f"    → Found {len(next_buttons)} navigation buttons")
                        
                        # Scroll through carousel up to 10 times to get all offers
                        for scroll_round in range(10):
                            # Extract visible offers
                            try:
                                visible_text = carousel_container.text
                                lines = [l.strip() for l in visible_text.split('\n') if l.strip()]
                                
                                i = 0
                                while i < len(lines):
                                    line = lines[i]
                                    
                                    # Check for "Best value" tag
                                    is_best_value = ((i > 0 and 'best value' in lines[i-1].lower()) or 
                                                   'best value' in line.lower())
                                    
                                    # Check if line contains discount
                                    if (('₹' in line and 'off' in line.lower()) or 
                                        ('%' in line and ('cashback' in line.lower() or 'off' in line.lower()))):
                                        
                                        offer_parts = []
                                        
                                        if is_best_value:
                                            offer_parts.append('[Best Value]')
                                        
                                        offer_parts.append(line)
                                        
                                        # Look ahead for bank name and card type (position-based, not name-based)
                                        # Pattern: Amount → "Apply" → Bank Name → Card Type
                                        details_found = 0
                                        for j in range(i+1, min(i+6, len(lines))):
                                            next_line = lines[j]
                                            
                                            # Skip UI text
                                            if (next_line in ['Apply', 'Apply Now', 'View Details', 'T&C', 'View All'] or
                                                'best value' in next_line.lower() or 
                                                'Bank offer' in next_line):
                                                continue
                                            
                                            # Stop if we hit another amount (next offer)
                                            if '₹' in next_line and 'off' in next_line.lower():
                                                break
                                            
                                            # Take next 2 non-skipped lines as bank name + card type
                                            if details_found < 2:
                                                offer_parts.append(next_line)
                                                details_found += 1
                                            else:
                                                break
                                        
                                        # Build offer text
                                        offer_text = ' - '.join(offer_parts)
                                        # Create unique key including card type to differentiate Credit vs Debit
                                        unique_key = ' - '.join(offer_parts[:3]).replace('[Best Value]', '').strip()[:80]
                                        
                                        if offer_text and len(offer_text) > 5 and unique_key not in seen_offers:
                                            seen_offers.add(unique_key)
                                            offers.append(offer_text)
                                            if verbose:
                                                print(f"    ✓ Offer {len(offers)}: {offer_text[:80]}")
                                    
                                    i += 1
                                    
                            except Exception as e:
                                pass
                            
                            # Try to scroll carousel
                            if next_buttons and scroll_round < 9:
                                clicked = False
                                changed = True
                                try:
                                    before_text = carousel_container.text
                                except Exception:
                                    before_text = None
                                for btn in next_buttons:
                                    try:
                                        if btn.is_displayed() and btn.is_enabled():
                                            # Multiple click strategies
                                            try:
                                                # Strategy 1: Scroll button into view and JS click
                                                driver.execute_script("arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});", btn)
                                                time.sleep(0.15)
                                                driver.execute_script("arguments[0].click();", btn)
                                                clicked = True
                                                if verbose:
                                                    print(f"    → Scrolled carousel (round {scroll_round + 1})")
                                                # Wait only until new offers render (max 1.5s)
                                                if before_text is not None:
                                                    changed = _wait_for_text_change(carousel_container, before_text)
                                                else:
                                                    time.sleep(1.5)
                                                break
                                            except:
                                                try:
                                                    # Strategy 2: Direct click
                                                    btn.click()
                                                    clicked = True
                                                    if before_text is not None:
                                                        changed = _wait_for_text_change(carousel_container, before_text)
                                                    else:
                                                        time.sleep(1.5)
                                                    if verbose:
                                                        print(f"    → Scrolled carousel (round {scroll_round + 1})")
                                                    break
                                                except:
                                                    continue
                                    except:
                                        continue

                                if not clicked:
                                    if verbose:
                                        print(f"    → Carousel scroll complete (no more buttons)")
                                    break
                                if not changed:
                                    # Click produced no new content: carousel is at its end.
                                    if verbose:
                                        print(f"    → Carousel end reached (no new content after click)")
                                    break
                            else:
                                if verbose and scroll_round >= 9:
                                    print(f"    → Reached maximum scroll attempts")
                                break
                        
                        break
                        
                    except Exception as e:
                        continue
                        
        except Exception as e:
            pass
        
        # Method 2: Fallback - extract from static section text
        if len(offers) < 2:
            try:
                bank_offer_headers = driver.find_elements(By.XPATH, 
                    "//*[contains(text(), 'Bank offer') or contains(text(), 'Bank Offer')]")
                
                if bank_offer_headers:
                    for header in bank_offer_headers[:2]:
                        try:
                            for level in [4, 5, 6]:
                                try:
                                    parent = header.find_element(By.XPATH, f"./ancestor::div[{level}]")
                                    section_text = parent.text
                                    
                                    if len(section_text) > 50:
                                        lines = [l.strip() for l in section_text.split('\n') if l.strip()]
                                        
                                        i = 0
                                        while i < len(lines):
                                            line = lines[i]
                                            
                                            is_best_value = ((i > 0 and 'best value' in lines[i-1].lower()) or 
                                                           'best value' in line.lower())
                                            
                                            if (('₹' in line and 'off' in line.lower()) or 
                                                ('%' in line and 'cashback' in line.lower())):
                                                
                                                offer_parts = []
                                                
                                                if is_best_value:
                                                    offer_parts.append('[Best Value]')
                                                
                                                offer_parts.append(line)
                                                
                                                for j in range(i+1, min(i+4, len(lines))):
                                                    next_line = lines[j]
                                                    if 'best value' in next_line.lower():
                                                        continue
                                                    if any(bank in next_line for bank in 
                                                          ['HDFC', 'ICICI', 'SBI', 'Axis', 'Kotak', 
                                                           'Paytm', 'Flipkart', 'Standard Chartered']):
                                                        offer_parts.append(next_line)
                                                    elif any(term in next_line.lower() for term in 
                                                            ['credit card', 'debit card', 'upi', 'cashback', 'includes']):
                                                        offer_parts.append(next_line)
                                                    elif next_line in ['Apply Now', 'View Details', 'T&C']:
                                                        break
                                                
                                                offer_text = ' - '.join(offer_parts)
                                                unique_key = offer_text.replace('[Best Value]', '').strip()[:50]
                                                
                                                if offer_text and len(offer_text) > 5 and unique_key not in seen_offers:
                                                    seen_offers.add(unique_key)
                                                    offers.append(offer_text)
                                            
                                            i += 1
                                        
                                        if len(offers) >= 5:
                                            break
                                            
                                except:
                                    continue
                            
                            if len(offers) >= 5:
                                break
                                
                        except:
                            continue
                            
            except Exception as e:
                pass
        
        # Method 3: HTML regex extraction as last resort
        if len(offers) < 2:
            try:
                bank_pos = html.find('Bank offer')
                if bank_pos > 0:
                    section = html[bank_pos:bank_pos+8000]
                    
                    amounts = re.findall(r'(₹\s*[\d,]+\s+off)', section, re.IGNORECASE)
                    banks = ['HDFC', 'ICICI', 'SBI', 'Axis', 'Kotak', 'Citibank', 
                            'Paytm', 'Flipkart', 'PhonePe']
                    
                    for amount in amounts[:8]:
                        amt_pos = section.find(amount)
                        context_before = section[max(0, amt_pos-300):amt_pos]
                        context_after = section[amt_pos:amt_pos+400]
                        
                        is_best_value = ('best value' in context_before.lower() or 
                                       'best value' in context_after[:200].lower())
                        
                        found_bank = None
                        for bank in banks:
                            if bank in context_after:
                                found_bank = bank
                                break
                        
                        offer_text = ""
                        if is_best_value:
                            offer_text = "[Best Value] "
                        offer_text += amount.strip()
                        if found_bank:
                            offer_text += f" - {found_bank}"
                        
                        unique_key = offer_text.replace('[Best Value]', '').strip()[:50]
                        if unique_key not in seen_offers:
                            seen_offers.add(unique_key)
                            offers.append(offer_text)
                    
                    # Also find percentage offers
                    percents = re.findall(r'(\d+%\s+(?:cashback|off))', section, re.IGNORECASE)
                    for pct in percents[:3]:
                        pct_clean = pct.strip()
                        if pct_clean not in seen_offers:
                            seen_offers.add(pct_clean)
                            offers.append(pct_clean)
                            
            except Exception as e:
                pass
        
        # Clean and return final list
        final_offers = []
        for offer in offers:
            clean = ' '.join(offer.split())
            clean = re.sub(r'•+', '•', clean)
            if len(clean) > 3 and len(clean) < 250:
                final_offers.append(clean)
        
        if verbose and final_offers:
            print(f"  [Bank Offers] Captured {len(final_offers)} total offers")
        
        return final_offers[:10]
        
    except Exception as e:
        return []
